"""专用连接所有者、有界就绪队列和跨外部执行阶段的收尾容量。"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import threading
from collections import deque
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import PurePath

from pydantic import BaseModel

from .capture_scope import capture_key, reject_capture_reentry


class StorageBusy(RuntimeError):
    code = "storage_busy"

    def __init__(self):
        super().__init__(self.code)


class StorageCancelled(RuntimeError):
    code = "cancelled"

    def __init__(self):
        super().__init__(self.code)


class StorageFailed(RuntimeError):
    code = "storage_owner_failed"

    def __init__(self):
        super().__init__(self.code)


@dataclasses.dataclass(frozen=True)
class StorageContext:
    companion_id: str
    scene: str = "public_group"
    conversation_id: str | None = None
    permissions: tuple[str, ...] = ()
    expected_revisions: tuple[tuple[str, str], ...] = ()


_context: ContextVar[StorageContext | None] = ContextVar("storage_context", default=None)


def current_storage_context():
    return _context.get()


def _snapshot(value):
    """只接受独立值；连接、游标、生成器及任意运行时对象不越过边界。"""
    if value is None or isinstance(
        value, (str, bytes, bool, int, float, date, datetime, timedelta, Enum, PurePath)
    ):
        return value
    if isinstance(value, dict):
        return {_snapshot(key): _snapshot(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return type(value)(_snapshot(item) for item in value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            _snapshot(getattr(value, field.name))
        return copy.deepcopy(value)
    if isinstance(value, BaseModel):
        for item in value.__dict__.values():
            _snapshot(item)
        for item in (value.__pydantic_private__ or {}).values():
            _snapshot(item)
        return value.model_copy(deep=True)
    raise TypeError("storage_result_not_snapshot")


class Finalizer:
    """仅由可信本体持有；未就绪凭证不在执行队列内。"""

    def __init__(self, owner):
        self.owner = owner
        self.busy = False
        self.released = False


class Command:
    def __init__(self, owner, function, context, finalizer):
        self.owner = owner
        self.function = function
        self.context = context
        self.finalizer = finalizer
        self._phase = "queued"
        self._cancelled = False
        self._future = owner.loop.create_future()
        # 断线后仍保留真实执行状态；异常被观察，不能产生无人检索的警告。
        self._future.add_done_callback(lambda future: future.exception())

    @property
    def phase(self):
        with self.owner._condition:
            return self._phase

    def cancel(self):
        with self.owner._condition:
            if self.finalizer is not None or self._phase in {"committing", "finished"}:
                return False
            self._cancelled = True
            self.owner._condition.notify_all()
            return True

    def checkpoint(self):
        with self.owner._condition:
            if self._cancelled and self._phase != "committing":
                raise StorageCancelled()

    def begin_commit(self):
        """由领域事务在提交前调用；取消与提交开始用同一控制锁裁决。"""
        with self.owner._condition:
            if self._cancelled:
                raise StorageCancelled()
            self._phase = "committing"

    async def result(self):
        # 等待者取消不是业务取消。需要取消时，调用方另行请求 cancel。
        return await asyncio.shield(self._future)


class StorageOwner:
    def __init__(
        self,
        factory,
        *,
        companion_id,
        clock,
        ordinary_capacity=64,
        finalizer_capacity=16,
        coordination_path=None,
    ):
        if ordinary_capacity < 1 or finalizer_capacity < 1:
            raise ValueError("invalid_storage_capacity")
        self.factory = factory
        self.companion_id = companion_id
        self.clock = clock
        self.ordinary_capacity = ordinary_capacity
        self.finalizer_capacity = finalizer_capacity
        self.loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._capture_key = (
            capture_key(coordination_path) if coordination_path is not None else None
        )
        self._condition = threading.Condition()
        self._queue = deque()
        self._ordinary = 0
        self._finalizers = set()
        self._accepting = True
        self._closing = False
        self._failed = False
        self._active = None
        self._ready = self.loop.create_future()
        self._closed = self.loop.create_future()
        self._closed.add_done_callback(lambda future: future.exception())
        self._thread = threading.Thread(target=self._run, name="clytie-storage-owner")

    @classmethod
    async def start(cls, factory, **kwargs):
        owner = cls(factory, **kwargs)
        owner._thread.start()
        try:
            await asyncio.shield(owner._ready)
        except BaseException:
            # 工厂尚未完成时也不遗弃所有者；结束后由原线程关闭资源。
            owner.begin_stop()
            with owner._condition:
                owner._closing = True
                owner._condition.notify_all()
            raise
        return owner

    def _validate_finalizer(self, token):
        if token.owner is not self or token.released or token not in self._finalizers:
            raise ValueError("invalid_storage_finalizer")

    def reserve_finalizer(self):
        with self._condition:
            if self._failed:
                raise StorageFailed()
            if not self._accepting or len(self._finalizers) >= self.finalizer_capacity:
                raise StorageBusy()
            token = Finalizer(self)
            self._finalizers.add(token)
            return token

    def release_finalizer(self, token, *, executor_finished, obligations_complete):
        with self._condition:
            if token.owner is not self:
                raise ValueError("invalid_storage_finalizer")
            if token.released:
                return
            self._validate_finalizer(token)
            if token.busy or not executor_finished or not obligations_complete:
                raise StorageBusy()
            token.released = True
            self._finalizers.remove(token)
            self._condition.notify_all()

    def submit(self, function, *, context=None, finalizer=None):
        if threading.get_ident() != self._loop_thread:
            raise RuntimeError("storage_thread_reentry")
        reject_capture_reentry(self._capture_key)
        context = context or StorageContext(self.companion_id)
        if not isinstance(context, StorageContext) or context.companion_id != self.companion_id:
            raise ValueError("storage_context_mismatch")
        context = _snapshot(context)
        with self._condition:
            if self._failed:
                raise StorageFailed()
            if finalizer is None:
                if not self._accepting or self._ordinary >= self.ordinary_capacity:
                    raise StorageBusy()
            else:
                self._validate_finalizer(finalizer)
                if finalizer.busy:
                    raise StorageBusy()
            command = Command(self, function, context, finalizer)
            if finalizer is None:
                self._ordinary += 1
            else:
                finalizer.busy = True
            self._queue.append(command)
            self._condition.notify_all()
            return command

    async def execute(self, function, **kwargs):
        return await self.submit(function, **kwargs).result()

    def status(self):
        with self._condition:
            return {
                "source": "owner_control_cache",
                "observed_at": self.clock.now().isoformat(),
                "ordinary": self._ordinary,
                "finalizers": len(self._finalizers),
                "queued": len(self._queue),
                "phase": self._active._phase if self._active else "idle",
                "accepting": self._accepting,
                "failed": self._failed,
            }

    def begin_stop(self):
        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    async def close(self):
        self.begin_stop()
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        await asyncio.shield(self._closed)

    def _deliver(self, future, result=None, error=None):
        def finish():
            if not future.done():
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(result)

        self.loop.call_soon_threadsafe(finish)

    def _finish(self, command, result=None, error=None):
        with self._condition:
            command._phase = "finished"
            if command.finalizer is None:
                self._ordinary -= 1
            else:
                command.finalizer.busy = False
            self._active = None
        self._deliver(command._future, result, error)

    def _run(self):
        resources = None
        failure = None
        try:
            resources = self.factory()
            self._deliver(self._ready)
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._queue or (self._closing and not self._finalizers)
                    )
                    if not self._queue:
                        break
                    command = self._queue.popleft()
                    self._active = command
                    command._phase = "running"
                binding = _context.set(command.context)
                try:
                    command.checkpoint()
                    result = _snapshot(command.function(resources, command))
                except Exception as exc:
                    self._finish(command, error=exc)
                else:
                    self._finish(command, result=result)
                finally:
                    _context.reset(binding)
        except BaseException:
            failure = StorageFailed()
            with self._condition:
                self._failed = True
                self._accepting = False
                abandoned = ([self._active] if self._active else []) + list(self._queue)
                self._queue.clear()
            for command in abandoned:
                self._finish(command, error=StorageFailed())
            self._deliver(self._ready, error=failure)
        finally:
            if resources is not None:
                try:
                    resources.close()
                except BaseException:
                    failure = StorageFailed()
                    with self._condition:
                        self._failed = True
                        self._accepting = False
            self._deliver(self._closed, error=failure)
