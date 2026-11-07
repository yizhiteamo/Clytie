"""跨库固定 WAL 读视图；后台分批物化，不复制活动 db/wal。"""

import asyncio
import sqlite3
import threading
from pathlib import Path

from .coordination import CaptureError


class CapturedGroup:
    def __init__(self, epoch, connections, captured_at):
        self.epoch = epoch
        self.connections = connections
        self.captured_at = captured_at
        self.closed = False
        self._materializing = False

    def close(self):
        if self._materializing:
            raise RuntimeError("materialization still active")
        if not self.closed:
            for db in self.connections.values():
                db.close()
            self.closed = True

    async def materialize(self, directory, *, pages=128, check=None):
        if self.closed or self._materializing or pages < 1:
            raise ValueError("snapshot_inconsistent")
        self._materializing = True
        cancelled = threading.Event()

        def checkpoint(*_):
            if cancelled.is_set():
                raise CaptureError("cancelled")
            if check:
                check()

        def copy():
            directory_path = Path(directory)
            directory_path.mkdir(parents=True, exist_ok=False)
            result = {}
            for name, source in self.connections.items():
                checkpoint()
                target = directory_path / (name + ".db")
                with target.open("xb"):
                    pass
                destination = sqlite3.connect(target)
                try:
                    source.backup(destination, pages=pages, progress=checkpoint, sleep=0)
                    if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise CaptureError("snapshot_inconsistent")
                finally:
                    destination.close()
                result[name] = target
            return result

        task = asyncio.create_task(asyncio.to_thread(copy))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            # 不将任务取消当成线程退出；收回执行者后才关闭源连接。
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise
        finally:
            self._materializing = False
            self.close()


async def capture_group(paths, coordinator, *, epoch, capture=None):
    """调用方已取得生命周期锁且组内所有写入都接入 coordinator。"""
    if not paths or not epoch:
        raise CaptureError("snapshot_inconsistent")
    connections = {}
    try:
        for name, path in sorted(paths.items()):
            if not name or not name.isascii() or not all(c.isalnum() or c == "_" for c in name):
                raise CaptureError("snapshot_inconsistent")
            db = sqlite3.connect(
                Path(path).resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=0,
                isolation_level=None,
                check_same_thread=False,
            )
            connections[name] = db
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                raise CaptureError("snapshot_unsupported")
        async with coordinator.capture() as checkpoint:
            for db in connections.values():
                checkpoint()
                db.execute("BEGIN")
                db.execute("SELECT count(*) FROM sqlite_schema").fetchone()
                checkpoint()
            if capture is not None:
                await capture(checkpoint)
                checkpoint()
            captured_at = coordinator.clock.now().isoformat()
        return CapturedGroup(epoch, connections, captured_at)
    except BaseException:
        for db in connections.values():
            db.close()
        raise
