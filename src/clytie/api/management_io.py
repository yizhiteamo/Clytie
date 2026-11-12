"""管理文件阶段的专用线程；不接收活动数据库连接或通用插件回调。"""

import asyncio
import threading
from concurrent.futures import Future
from contextlib import ExitStack, asynccontextmanager
from queue import Queue
from uuid import uuid4

from clytie.backup.io_client import current_checks, current_client, io_checks, io_scope
from clytie.clock import SystemClock
from clytie.storage.owner import _snapshot

from .management_journal import current


async def settle(future):
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    return future.result()


class FileCommands:
    def __init__(self, clock):
        self.clock = clock
        self._requests = Queue(maxsize=1)
        self._active = False
        self._closed = False
        self.thread = threading.Thread(target=self._run, name="clytie-management-files")
        self.thread.start()

    def _run(self):
        while True:
            function, journal, client, checks, future = self._requests.get()
            if function is None:
                future.set_result(None)
                return
            binding = current.set(journal)
            try:
                with ExitStack() as scope:
                    scope.enter_context(io_scope(client))
                    for check in checks:
                        scope.enter_context(io_checks(check))
                    result = _snapshot(function())
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
            finally:
                current.reset(binding)

    async def run(self, function, *args, **kwargs):
        if self._active or self._closed:
            raise RuntimeError("management_file_command_busy")
        self._active = True
        result = Future()
        self._requests.put_nowait(
            (
                lambda: function(*args, **kwargs),
                current.get(),
                current_client(),
                current_checks(),
                result,
            )
        )
        future = asyncio.wrap_future(result)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # 请求任务取消不能抛弃仍持文件锁或临时数据库的执行者。
            try:
                await settle(future)
            except Exception:
                pass
            raise
        finally:
            self._active = False

    async def close(self):
        if self._closed:
            return
        if self._active:
            raise RuntimeError("management_file_command_active")
        self._closed = True
        result = Future()
        self._requests.put_nowait((None, None, None, (), result))
        await settle(asyncio.wrap_future(result))
        while self.thread.is_alive():
            await self.clock.sleep(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()


async def file_call(store, function, *args, **kwargs):
    """仅文件阶段转交线程；活动及离线业务数据库仍属于原执行者。"""
    from clytie.storage.handle import StoreHandle

    if not isinstance(store, StoreHandle) and current_client() is None:
        return function(*args, **kwargs)
    async with FileCommands(store.clock if store is not None else SystemClock()) as worker:
        return await worker.run(function, *args, **kwargs)


@asynccontextmanager
async def owned_snapshot(store, home, companion_id):
    """固定活动库的只读视图；复制和临时库连接始终属于快照执行者。"""
    from clytie.backup.capture import capture_group
    from clytie.backup.coordination import companion_coordinator
    from clytie.plugin_host.snapshots import contained

    directory = contained(home, home / "run/memory-temp" / ("owned-" + uuid4().hex))
    coordinator = store.coordination or companion_coordinator(home, companion_id, store.clock)
    journal = current.get()
    group = await capture_group(
        {"memory": store.path}, coordinator, epoch=uuid4().hex, owner=store._owner
    )
    try:
        paths = await group.materialize(directory, check=journal.checkpoint if journal else None)
        yield paths["memory"]
    finally:
        await group.close()

        def cleanup():
            for name in ("memory.db", "memory.db-journal", "memory.db-wal", "memory.db-shm"):
                path = contained(home, directory / name)
                if path.exists():
                    path.unlink()
            if directory.exists():
                directory.rmdir()

        await file_call(store, cleanup)


async def management_command(store, function, *args, **kwargs):
    from clytie.backup.coordination import run_sync
    from clytie.storage.finalization import finalizer_scope

    journal = current.get()
    token = getattr(journal, "storage_finalizer", None) if journal is not None else None

    async def execute():
        with finalizer_scope(token):
            return await run_sync(store, function, *args, **kwargs)

    command = asyncio.create_task(execute())
    try:
        return await asyncio.shield(command)
    except asyncio.CancelledError:
        try:
            await settle(command)
        except Exception:
            pass
        raise
