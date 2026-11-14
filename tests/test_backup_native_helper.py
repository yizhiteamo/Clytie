import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from clytie.backup.io_budget import IoBudgetError
from clytie.backup.io_client import IoClient, io_checks, io_scope
from clytie.backup.native_sqlite import native_checks, validation_stage


class Transport:
    def __init__(self):
        self.actions = []

    def __call__(self, request, *, cleanup=False):
        self.actions.append(request["action"])
        return {}


class Cancelled(BaseException):
    pass


LONG_QUERY = """
WITH RECURSIVE rows(value) AS (
    SELECT 1 UNION ALL SELECT value + 1 FROM rows WHERE value < (10 * 1000)
) SELECT sum(value) FROM rows
"""


def test_stage_covers_connection_close_and_does_not_charge(tmp_path):
    transport = Transport()
    with io_scope(IoClient(transport)), validation_stage():
        assert transport.actions == ["acquire"]
        db = sqlite3.connect(tmp_path / "fictional.sqlite")
        try:
            with native_checks(db):
                assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        finally:
            db.close()
        assert transport.actions == ["acquire"]
    assert transport.actions == ["acquire", "release"]


def test_nested_stage_reuses_held_client_when_byte_scope_disabled():
    transport = Transport()
    client = IoClient(transport)
    with io_scope(client), client.stage(), io_scope(None), validation_stage():
        with validation_stage():
            assert transport.actions == ["acquire"]
        assert transport.actions == ["acquire"]
    assert transport.actions == ["acquire", "release"]


def test_different_client_cannot_reuse_stage():
    with io_scope(IoClient(Transport())), validation_stage():
        with io_scope(IoClient(Transport())):
            with pytest.raises(IoBudgetError, match="backup_io_stage_context_mismatch"):
                with validation_stage():
                    pytest.fail("entered a different client's held stage")


@pytest.mark.parametrize("when", ["before", "during", "after"])
def test_checks_preserve_original_baseexception_and_remove_handler(when):
    original = Cancelled("fictional cancellation")
    calls = 0
    entered = False
    completed = False

    def check():
        nonlocal calls
        calls += 1
        if when == "before" or (when == "during" and calls > 1) or completed:
            raise original

    db = sqlite3.connect(":memory:")
    try:
        with io_checks(check):
            with pytest.raises(Cancelled) as caught:
                with native_checks(db):
                    entered = True
                    db.execute(LONG_QUERY).fetchone()
                    completed = True
            assert caught.value is original
            assert entered is (when != "before")
            assert completed is (when == "after")
            previous = calls
            assert db.execute(LONG_QUERY).fetchone() == (sum(range(1, 10 * 1000 + 1)),)
            assert calls == previous
    finally:
        db.close()


def test_sqlite_error_does_not_leave_progress_handler_installed():
    calls = []
    db = sqlite3.connect(":memory:")
    try:
        with io_checks(lambda: calls.append("check")):
            with pytest.raises(sqlite3.OperationalError):
                with native_checks(db):
                    db.execute("SELECT value FROM missing_fictional_table")
            previous = len(calls)
            assert db.execute(LONG_QUERY).fetchone() == (sum(range(1, 10 * 1000 + 1)),)
            assert len(calls) == previous
    finally:
        db.close()


def test_caught_sqlite_interruption_cannot_hide_original_cancellation():
    original = Cancelled("fictional cancellation")
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise original

    db = sqlite3.connect(":memory:")
    try:
        with io_checks(check), pytest.raises(Cancelled) as caught:
            with native_checks(db):
                with pytest.raises(sqlite3.OperationalError, match="interrupted"):
                    db.execute(LONG_QUERY).fetchone()
        assert caught.value is original
    finally:
        db.close()


def test_blocked_native_call_keeps_stage_until_return_and_connection_close():
    transport = Transport()
    inside_call = threading.Event()
    let_return = threading.Event()
    cancelled = threading.Event()
    events = []
    original = Cancelled("fictional cancellation")

    def check():
        if cancelled.is_set():
            raise original

    def blocked():
        inside_call.set()
        assert let_return.wait(10)
        events.append("returned")
        return 1

    def worker():
        with io_scope(IoClient(transport)), io_checks(check), validation_stage():
            db = sqlite3.connect(":memory:")
            try:
                db.create_function("fictional_block", 0, blocked)
                with native_checks(db):
                    db.execute("SELECT fictional_block()").fetchone()
            finally:
                db.close()
                events.append("closed")
                assert transport.actions == ["acquire"]

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker)
        try:
            assert inside_call.wait(10)
            cancelled.set()
            assert not future.done()
            assert events == []
            assert transport.actions == ["acquire"]
        finally:
            let_return.set()
        with pytest.raises(Cancelled) as caught:
            future.result(timeout=10)
    assert caught.value is original
    assert events == ["returned", "closed"]
    assert transport.actions == ["acquire", "release"]


async def test_validation_stage_rejects_event_loop_budget_wait():
    transport = Transport()
    with io_scope(IoClient(transport)):
        with pytest.raises(IoBudgetError, match="backup_io_on_event_loop"):
            with validation_stage():
                pytest.fail("blocking validation stage entered event loop")
    assert transport.actions == []
