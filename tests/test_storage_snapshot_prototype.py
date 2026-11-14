"""Issue #72 可执行 SQLite 原型；不是生产捕获接线的替代证据。"""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing

import pytest

from clytie.backup.coordination import CaptureCoordinator, CaptureError
from clytie.sdk.testing import MockClock


class Writer:
    """连接创建、事务与关闭均在专用写线程。"""

    def __init__(self, root, coordinator):
        self.coordinator = coordinator
        self.state = root / "state.txt"
        self.paths = {name: root / f"{name}.db" for name in ("memory", "ledger")}
        self.connections = {name: sqlite3.connect(path) for name, path in self.paths.items()}
        for db in self.connections.values():
            assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            db.execute("PRAGMA busy_timeout=0")
            db.execute("CREATE TABLE facts(generation INTEGER, payload BLOB)")
            db.executemany("INSERT INTO facts VALUES(0, zeroblob(4096))", [()] * 256)
            db.commit()
        self.write(1)
        self.thread = threading.get_ident()

    def write(self, generation):
        with self.coordinator.commit():
            for db in self.connections.values():
                with db:
                    db.execute("UPDATE facts SET generation=?", (generation,))
            self.state.write_text(str(generation), encoding="utf-8")

    def checkpoint(self):
        return [
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            for db in self.connections.values()
        ]

    def close(self):
        assert threading.get_ident() == self.thread
        for db in self.connections.values():
            db.close()


def copy_views(writer, coordinator, target, pinned, resume, *, cancel=False, timeout=False):
    """单个专用读线程从开库到关库；不关闭 SQLite 的线程检查。"""
    target.mkdir()
    batches = []
    with ExitStack() as stack:
        readers = {
            name: stack.enter_context(
                closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0))
            )
            for name, path in writer.paths.items()
        }
        with coordinator.commit():
            started = coordinator.clock.monotonic()
            for db in readers.values():
                db.execute("BEGIN")
                assert db.execute("SELECT generation FROM facts LIMIT 1").fetchone()[0] == 1
                if timeout:
                    pinned.set()
                    assert resume.wait(10), "test driver did not release partial capture"
                if coordinator.clock.monotonic() - started >= coordinator.hold_seconds:
                    raise CaptureError("capture_busy")
            state = writer.state.read_text(encoding="utf-8")

        def progress(status, remaining, total):
            batches.append((status, remaining, total))
            if len(batches) == 1:
                pinned.set()
                assert resume.wait(10), "test driver did not release copying"
            if cancel and len(batches) >= 3:
                raise CaptureError("cancelled")

        for name, db in readers.items():
            with closing(sqlite3.connect(target / f"{name}.db")) as output:
                db.backup(output, pages=1, progress=progress, sleep=0)
                assert output.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        return state, batches, threading.get_ident()


@pytest.mark.parametrize("cancel", [False, True])
def test_owned_read_views_remain_fixed_during_writes_and_release_wal(tmp_path, cancel):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    pinned, resume = threading.Event(), threading.Event()
    # 两个独立单线程执行器仅用于原型；不转交连接到公共池。
    with (
        ThreadPoolExecutor(max_workers=1) as writes,
        ThreadPoolExecutor(max_workers=1) as snapshots,
    ):
        writer = writes.submit(Writer, tmp_path, coordinator).result(10)
        try:
            with pytest.raises(sqlite3.ProgrammingError):
                writer.connections["memory"].execute("SELECT 1")
            copied = snapshots.submit(
                copy_views,
                writer,
                coordinator,
                tmp_path / "snapshot",
                pinned,
                resume,
                cancel=cancel,
            )
            try:
                assert pinned.wait(10)
                for generation in range(2, 6):
                    writes.submit(writer.write, generation).result(10)
                # 复制第一批时仍在原读事务；新写已提交，旧 WAL 不能截断。
                assert all(row[0] == 1 for row in writes.submit(writer.checkpoint).result(10))
            finally:
                resume.set()
            if cancel:
                with pytest.raises(CaptureError, match="cancelled"):
                    copied.result(10)
            else:
                state, batches, reader_thread = copied.result(10)
                assert reader_thread != writer.thread
                assert state == "1"
                assert len(batches) > 3
                for name in writer.paths:
                    with closing(sqlite3.connect(tmp_path / "snapshot" / f"{name}.db")) as db:
                        assert db.execute("SELECT DISTINCT generation FROM facts").fetchall() == [
                            (1,)
                        ]
            assert writes.submit(writer.checkpoint).result(10) == [(0, 0, 0), (0, 0, 0)]
            writes.submit(writer.write, 6).result(10)
        finally:
            resume.set()
            writes.submit(writer.close).result(10)


def test_mockclock_capture_timeout_closes_partially_pinned_readers(tmp_path):
    clock = MockClock()
    coordinator = CaptureCoordinator(tmp_path / "capture.lock", clock)
    pinned, resume = threading.Event(), threading.Event()
    with (
        ThreadPoolExecutor(max_workers=1) as writes,
        ThreadPoolExecutor(max_workers=1) as snapshots,
    ):
        writer = writes.submit(Writer, tmp_path, coordinator).result(10)
        try:
            copied = snapshots.submit(
                copy_views, writer, coordinator, tmp_path / "snapshot", pinned, resume, timeout=True
            )
            try:
                assert pinned.wait(10)
                clock.advance(0.25)
            finally:
                resume.set()
            with pytest.raises(CaptureError, match="capture_busy"):
                copied.result(10)
            writes.submit(writer.write, 2).result(10)
            assert writes.submit(writer.checkpoint).result(10) == [(0, 0, 0), (0, 0, 0)]
        finally:
            resume.set()
            writes.submit(writer.close).result(10)
