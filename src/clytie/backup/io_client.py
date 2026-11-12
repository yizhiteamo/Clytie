"""同步重 I/O 的分块客户端；预算只由既有控制通道的实例调度器授予。"""

import asyncio
import os
import stat
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from uuid import uuid4

from .io_budget import CHUNK, IoBudgetError

_client = ContextVar("backup_io_client", default=None)
_stage = ContextVar("backup_io_stage", default=None)


@contextmanager
def io_scope(client):
    binding = _client.set(client)
    try:
        yield
    finally:
        _client.reset(binding)


def current_client():
    return _client.get()


class IoClient:
    def __init__(self, request):
        # request 与原操作/真实执行者绑定；不能携带客户端选择的其他执行者。
        self.request = request

    @contextmanager
    def stage(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise IoBudgetError("backup_io_on_event_loop")
        existing = _stage.get()
        if existing is not None:
            if existing["client"] is not self or existing["thread"] != threading.get_ident():
                raise IoBudgetError("backup_io_stage_context_mismatch")
            # 同一同步阶段内的小帮手共用槽位，不申请下一阶段并等待自己。
            yield
            return
        key = uuid4().hex
        state = {"client": self, "id": key, "thread": threading.get_ident(), "sequence": 0}
        binding = _stage.set(state)
        try:
            self.request({"action": "acquire", "stage": key})
            yield
        finally:
            try:
                # 授予响应丢失也确认本阶段不再执行 I/O，不能丢下虚假占用。
                self.request({"action": "release", "stage": key}, cleanup=True)
            finally:
                _stage.reset(binding)

    def charge(self, direction, size):
        state = _stage.get()
        if state is None:
            raise IoBudgetError("backup_io_stage_required")
        if state["client"] is not self or state["thread"] != threading.get_ident():
            raise IoBudgetError("backup_io_stage_context_mismatch")
        state["sequence"] += 1
        self.request(
            {
                "action": "charge",
                "stage": state["id"],
                "sequence": state["sequence"],
                "direction": direction,
                "size": size,
            }
        )


def heavy_io(function):
    """只用于同步门闩外阶段；必要发布/恢复收尾不得标此装饰器。"""
    if asyncio.iscoroutinefunction(function):
        raise TypeError("backup_io_async_stage_forbidden")

    @wraps(function)
    def wrapped(*args, **kwargs):
        client = current_client()
        if client is None:
            return function(*args, **kwargs)
        with client.stage():
            return function(*args, **kwargs)

    return wrapped


def io_read(stream, count):
    client = current_client()
    if client is None:
        return stream.read(count)
    if type(count) is not int or count < 0:
        raise IoBudgetError("backup_io_unbounded_read")
    parts = []
    while count:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise IoBudgetError("backup_io_file_required")
        amount = min(CHUNK, count, max(0, info.st_size - stream.tell()))
        if not amount:
            break
        client.charge("read", amount)
        block = stream.read(amount)
        if len(block) != amount:
            raise IoBudgetError("backup_io_source_changed")
        parts.append(block)
        count -= amount
    return b"".join(parts)


def io_write(stream, data):
    client = current_client()
    if client is None:
        return stream.write(data)
    value = memoryview(data)
    for offset in range(0, len(value), CHUNK):
        block = value[offset : offset + CHUNK]
        client.charge("write", len(block))
        if stream.write(block) != len(block):
            raise IoBudgetError("backup_io_short_write")
    return len(value)
