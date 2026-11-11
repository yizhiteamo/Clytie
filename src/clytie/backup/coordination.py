"""跨进程短提交门闩；锁顺序由组装层固定。"""

import asyncio
import os
import threading
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from weakref import WeakValueDictionary

from clytie.plugin_host.locking import OperationBusy, file_lock
from clytie.plugin_host.snapshots import contained, identifier
from clytie.storage.capture_scope import capture_key, reject_capture_reentry

_locals = WeakValueDictionary()
_locals_guard = threading.Lock()


def _owner():
    try:
        return asyncio.current_task() or threading.get_ident()
    except RuntimeError:
        return threading.get_ident()


@asynccontextmanager
async def admission(store):
    """异步取得门闩，之后只执行同步原子操作；没有探测后重取窗口。"""
    coordinator = getattr(store, "coordination", None)
    if coordinator is None:
        yield
    else:
        reject_capture_reentry(capture_key(coordinator.path))
        async with coordinator.admit():
            yield


async def run_sync(store, function, *args, **kwargs):
    from clytie.storage.handle import StoreHandle

    if isinstance(store, StoreHandle):
        return await store.invoke(function, *args, **kwargs)
    async with admission(store):
        return function(*args, **kwargs)


@asynccontextmanager
async def transaction(store):
    async with admission(store):
        with store.transaction():
            yield


def companion_coordinator(home, cid, clock):
    home = Path(home).resolve()
    path = contained(home, home / "companions" / identifier(cid) / "backup-capture.lock")
    return CaptureCoordinator(path, clock)


class CaptureError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class CaptureCoordinator:
    def __init__(self, path, clock, *, wait_seconds=2.0, hold_seconds=0.25):
        self.path, self.clock = path, clock
        self.wait_seconds, self.hold_seconds = wait_seconds, hold_seconds
        self.max_write_wait = 0.0
        key = os.path.normcase(str(Path(path).resolve()))
        with _locals_guard:
            self._local = _locals.get(key)
            if self._local is None:
                self._local = threading.local()
                _locals[key] = self._local

    def _lock(self):
        return file_lock(self.path, {"operation": "backup_capture"})

    @contextmanager
    def commit(self):
        """同步事务入口；同一线程可把多库逻辑提交包在外层门闩中。"""
        if getattr(self._local, "depth", 0) and self._local.owner == _owner():
            self._local.depth += 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return
        started = self.clock.monotonic()
        waiter = threading.Event()
        # 第二上限避免错误/冻结的测试时钟导致无界阻塞。
        for _ in range(max(1, int(self.wait_seconds / 0.001) + 1)):
            manager = self._lock()
            try:
                manager.__enter__()
            except OperationBusy:
                if self.clock.monotonic() - started >= self.wait_seconds:
                    raise CaptureError("capture_busy") from None
                waiter.wait(0.001)
            else:
                break
        else:
            raise CaptureError("capture_busy")
        self.max_write_wait = max(self.max_write_wait, self.clock.monotonic() - started)
        self._local.depth = 1
        self._local.owner = _owner()
        try:
            yield
        finally:
            self._local.depth = 0
            manager.__exit__(None, None, None)

    @asynccontextmanager
    async def admit(self):
        if getattr(self._local, "depth", 0) and self._local.owner == _owner():
            yield
            return
        started = self.clock.monotonic()
        while True:
            manager = self._lock()
            try:
                manager.__enter__()
            except OperationBusy:
                if self.clock.monotonic() - started >= self.wait_seconds:
                    raise CaptureError("capture_busy") from None
                await self.clock.sleep(min(0.01, self.wait_seconds))
                continue
            break
        self.max_write_wait = max(self.max_write_wait, self.clock.monotonic() - started)
        self._local.depth, self._local.owner = 1, _owner()
        try:
            yield
        finally:
            self._local.depth = 0
            self._local.owner = None
            manager.__exit__(None, None, None)

    @asynccontextmanager
    async def capture(self):
        started = self.clock.monotonic()
        while True:
            if self.clock.monotonic() - started >= self.wait_seconds:
                raise CaptureError("capture_busy")
            manager = self._lock()
            acquired = self.clock.monotonic()
            try:
                manager.__enter__()
            except OperationBusy:
                await self.clock.sleep(min(0.01, self.wait_seconds))
                continue
            break

        def checkpoint():
            if self.clock.monotonic() - acquired >= self.hold_seconds:
                raise CaptureError("capture_busy")

        try:
            yield checkpoint
            checkpoint()
        finally:
            manager.__exit__(None, None, None)
