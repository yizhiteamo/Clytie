"""跨库固定 WAL 读视图；专用线程拥有连接和捕获锁的完整生命周期。"""

import asyncio
import sqlite3
import threading
from concurrent.futures import Future
from contextlib import nullcontext
from pathlib import Path
from queue import Queue

from clytie.plugin_host.locking import OperationBusy
from clytie.storage.capture_scope import capture_scope
from clytie.storage.owner import StorageBusy

from .coordination import CaptureError
from .view_limits import MAX_VIEW_SECONDS, ViewLimits


async def _settle(future):
    """取消等待者不等于执行者退出；保留执行责任直到真实结果到达。"""
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    return future.result()


class _SnapshotWorker:
    def __init__(self, paths, coordinator):
        self.paths, self.coordinator = dict(paths), coordinator
        self.started = coordinator.clock.monotonic()
        self.acquired = None
        self.cancelled = threading.Event()
        self._connections = {}
        self._manager = None
        self._release_error = None
        self._view_limits = None
        self.limit_error = None
        self._calls = asyncio.Lock()
        self._requests = Queue(maxsize=1)
        self.thread = threading.Thread(target=self._run, name="clytie-snapshot")
        self.thread.start()

    def _run(self):
        while True:
            command, args, future = self._requests.get()
            try:
                result = getattr(self, "_" + command)(*args)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
            if command == "close":
                return

    async def call(self, command, *args):
        async with self._calls:
            return await self._call(command, *args)

    async def _call(self, command, *args):
        result = Future()
        self._requests.put_nowait((command, args, result))
        future = asyncio.wrap_future(result)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            self.cancelled.set()
            try:
                await _settle(future)
            except Exception:
                pass
            raise

    def checkpoint(self):
        if self.cancelled.is_set():
            raise CaptureError("cancelled")
        if (
            self.acquired is not None
            and self.coordinator.clock.monotonic() - self.acquired >= self.coordinator.hold_seconds
        ):
            raise CaptureError("capture_busy")

    def _open(self):
        for name, path in sorted(self.paths.items()):
            self.checkpoint()
            db = sqlite3.connect(
                Path(path).resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=0,
                isolation_level=None,
            )
            self._connections[name] = db
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                raise CaptureError("snapshot_unsupported")

    def _capture(self):
        self.checkpoint()
        clock = self.coordinator.clock
        if clock.monotonic() - self.started >= self.coordinator.wait_seconds:
            raise CaptureError("capture_busy")
        manager = self.coordinator._lock()
        acquired = clock.monotonic()
        try:
            manager.__enter__()
        except OperationBusy:
            return False
        self._manager, self.acquired = manager, acquired
        # 不可抢占的锁文件 I/O 若迟到，不能发布成功或重新开始期限。
        if clock.monotonic() - self.started >= self.coordinator.wait_seconds:
            raise CaptureError("capture_busy")
        self.checkpoint()
        pinned_at = clock.monotonic()
        for db in self._connections.values():
            self.checkpoint()
            db.execute("BEGIN")
            db.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            self.checkpoint()
        self._view_limits = ViewLimits(self.paths, clock, pinned_at=pinned_at)
        self.checkpoint()
        return True

    def _release(self):
        if self._manager is not None:
            manager, self._manager = self._manager, None
            try:
                manager.__exit__(None, None, None)
            except BaseException as exc:
                self._release_error = exc
                raise
        if self._release_error is not None:
            raise self._release_error

    def _copy(self, directory, pages, check):
        def checkpoint(*_):
            if self.limit_error is not None:
                raise self.limit_error
            if self.cancelled.is_set():
                raise CaptureError("cancelled")
            if check:
                check()
            if self._view_limits is not None:
                self._view_limits.check()
            if self.cancelled.is_set():
                raise CaptureError("cancelled")

        checkpoint()
        directory_path = Path(directory)
        directory_path.mkdir(parents=True, exist_ok=False)
        result = {}
        for name, source in self._connections.items():
            checkpoint()
            target = directory_path / (name + ".db")
            with target.open("xb"):
                pass
            destination = sqlite3.connect(target)
            try:
                source.backup(destination, pages=pages, progress=checkpoint, sleep=0)
                checkpoint()
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise CaptureError("snapshot_inconsistent")
            finally:
                destination.close()
            result[name] = target
        checkpoint()
        return result

    def _check_view(self):
        if self._view_limits is not None:
            self._view_limits.check()

    def _close(self):
        error = None
        try:
            self._release()
        except BaseException as exc:
            error = exc
        for db in self._connections.values():
            try:
                db.close()
            except BaseException as exc:
                error = error or exc
        if error is not None:
            raise error
        self._connections.clear()


class CapturedGroup:
    def __init__(self, epoch, worker, captured_at=None, ticket=None):
        self.epoch, self._worker, self.captured_at = epoch, worker, captured_at
        self.closed = False
        self._materializing = False
        self._closing = None
        self._ticket = ticket
        self._watcher = None

    def watch(self):
        self._watcher = asyncio.create_task(self._watch())

    async def _watch(self):
        worker = self._worker
        try:
            while True:
                await worker.coordinator.clock.sleep(0.1)
                limits = worker._view_limits
                if limits and limits.clock.monotonic() - limits.pinned_at >= MAX_VIEW_SECONDS:
                    raise CaptureError("snapshot_view_expired")
                # 复制期间由其批次/预算等待检查 WAL；闲置期间仍定期核验。
                if not self._materializing:
                    await worker.call("check_view")
        except CaptureError as exc:
            worker.limit_error = exc
            worker.cancelled.set()
            if not self._materializing and self._closing is None:
                self._closing = asyncio.create_task(self._finish_close())
        except (OSError, ValueError):
            worker.limit_error = CaptureError("snapshot_wal_unconfirmed")
            worker.cancelled.set()
            if not self._materializing and self._closing is None:
                self._closing = asyncio.create_task(self._finish_close())

    @property
    def resource_pending(self):
        return not self.closed or self._worker.thread.is_alive()

    async def _finish_close(self):
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
        try:
            await self._worker.call("close")
        finally:
            # 关闭完成通知与线程真正退出分开核对；事件循环不执行 join。
            while self._worker.thread.is_alive():
                await self._worker.coordinator.clock.sleep(0)
        self.closed = True
        if self._ticket is not None:
            self._ticket.release()

    async def close(self):
        if self._materializing:
            raise RuntimeError("materialization still active")
        if self._closing is None:
            self._closing = asyncio.create_task(self._finish_close())
        try:
            await asyncio.shield(self._closing)
        except asyncio.CancelledError:
            try:
                await _settle(self._closing)
            except Exception:
                pass
            raise

    async def materialize(self, directory, *, pages=128, check=None):
        if self._worker.limit_error is not None:
            await self.close()
            raise self._worker.limit_error
        if self.closed or self._closing is not None or self._materializing or pages < 1:
            raise ValueError("snapshot_inconsistent")
        self._materializing = True
        try:
            return await self._worker.call("copy", directory, pages, check)
        finally:
            self._materializing = False
            await self.close()


async def capture_group(paths, coordinator, *, epoch, capture=None, owner=None):
    """调用方持有生命周期锁；数据库和文件门闩仅由快照线程访问。"""
    if (
        not paths
        or not epoch
        or any(
            not name or not name.isascii() or not all(c.isalnum() or c == "_" for c in name)
            for name in paths
        )
    ):
        raise CaptureError("snapshot_inconsistent")
    try:
        ticket = owner.reserve_capture() if owner is not None else None
    except StorageBusy:
        raise CaptureError("capture_busy") from None
    try:
        worker = _SnapshotWorker(paths, coordinator)
    except BaseException:
        if ticket is not None:
            ticket.release()
        raise
    group = CapturedGroup(epoch, worker, ticket=ticket)
    try:
        await worker.call("open")
        barrier = (
            ticket.barrier(worker.started + coordinator.wait_seconds) if ticket else nullcontext()
        )
        async with barrier:
            try:
                while not await worker.call("capture"):
                    remaining = coordinator.wait_seconds - (
                        coordinator.clock.monotonic() - worker.started
                    )
                    if remaining <= 0:
                        raise CaptureError("capture_busy")
                    await coordinator.clock.sleep(min(0.01, remaining))
                if capture is not None:
                    with capture_scope(coordinator.path):
                        await capture(worker.checkpoint)
                worker.checkpoint()
                group.captured_at = coordinator.clock.now().isoformat()
                await worker.call("release")
                # 释放若阻塞跨过持有期限，也不能把迟到视图当作成功。
                worker.checkpoint()
            except BaseException:
                worker.cancelled.set()
                await group.close()
                raise
        group.watch()
        return group
    except StorageBusy:
        worker.cancelled.set()
        await group.close()
        raise CaptureError("capture_busy") from None
    except BaseException:
        worker.cancelled.set()
        await group.close()
        raise
