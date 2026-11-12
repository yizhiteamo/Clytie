import asyncio
import gc
import json
import os
import sqlite3
import threading
import weakref
from types import SimpleNamespace

import pytest
from tests.test_measurement_evidence import load_tool

observation = load_tool("measurement_observation")
load_tool("measurement_native_observation")
recording = load_tool("measurement_recording")


def rows(recorder):
    return [json.loads(line) for line in recorder.rows["storage-phases.jsonl"].splitlines()]


def test_real_sqlite_commit_rollback_and_fsync_are_observed_without_content(tmp_path):
    recorder = recording.Recorder()
    observer = observation.Observation(tmp_path, recorder, origin=0, timer=lambda: 1)
    with observer.install(), observation.request_scope(7):
        db = sqlite3.connect(tmp_path / "fictional.db")
        try:
            db.execute("CREATE TABLE fixture(value)")
            db.execute("INSERT INTO fixture VALUES (?)", ("fictional private text",))
            db.commit()
            db.execute("INSERT INTO fixture VALUES ('rolled back')")
            db.rollback()
            with db:
                db.execute("INSERT INTO fixture VALUES ('context commit')")
            with pytest.raises(ValueError), db:
                db.execute("INSERT INTO fixture VALUES ('context rollback')")
                raise ValueError("fictional exception content")
            assert db.execute("SELECT count(*) FROM fixture").fetchone()[0] == 2
            with (tmp_path / "fixture.bin").open("wb") as stream:
                stream.write(b"fictional bytes")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            db.close()
    assert [row["phase"] for row in rows(recorder)] == [
        "commit",
        "rollback",
        "commit",
        "rollback",
        "fsync",
    ]
    assert all(row["request_id"] == 7 for row in rows(recorder))
    assert b"fictional" not in recorder.rows["storage-phases.jsonl"]


def test_observed_connection_preserves_thread_check_and_owner_close(tmp_path):
    recorder = recording.Recorder()
    observer = observation.Observation(tmp_path, recorder, origin=0, timer=lambda: 1)
    failures = []
    with observer.install():
        db = sqlite3.connect(tmp_path / "fixture.db")

        def wrong_thread():
            try:
                db.commit()
            except sqlite3.ProgrammingError:
                failures.append(True)

        thread = threading.Thread(target=wrong_thread)
        thread.start()
        thread.join(2)
        assert not thread.is_alive() and failures == [True]
        db.close()
    assert rows(recorder)[0]["outcome"] == "failed"


def test_observer_refuses_other_tree_and_custom_factory(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    observer = observation.Observation(fixture, recording.Recorder(), origin=0)
    outside = tmp_path / "not_fixture.db"
    with observer.install():
        with pytest.raises(ValueError, match="outside_fixture"):
            sqlite3.connect(outside)
        with pytest.raises(ValueError, match="factory_unsupported"):
            sqlite3.connect(fixture / "within.db", factory=lambda *_: None)
    assert not outside.exists()


def test_readonly_uri_keeps_readonly_policy(tmp_path):
    path = tmp_path / "fictional.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE fixture(value)")
    observer = observation.Observation(tmp_path, recording.Recorder(), origin=0)
    with observer.install():
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                db.execute("INSERT INTO fixture VALUES ('forbidden')")
        finally:
            db.close()


async def test_storage_command_timing_keeps_request_context_on_owner_thread(tmp_path):
    from tests.test_storage_owner import create

    owner, _ = await create(tmp_path)
    recorder = recording.Recorder()
    observer = observation.Observation(tmp_path, recorder, origin=0, timer=lambda: 1)
    try:
        with observer.observe_owner(owner), observation.request_scope(7):
            assert await owner.execute(lambda *_: 3) == 3
        result = rows(recorder)
        assert [r["phase"] for r in result] == ["queue", "execute"]
        assert all(r["request_id"] == 7 and r["outcome"] == "ok" for r in result)
    finally:
        await owner.close()


async def test_storage_queue_rejection_and_finalizer_are_not_dropped(tmp_path):
    from tests.test_storage_owner import create

    from clytie.storage.owner import StorageBusy

    owner, _ = await create(tmp_path, ordinary=1)
    recorder = recording.Recorder()
    observer = observation.Observation(tmp_path, recorder, origin=0, timer=lambda: 1)
    release = threading.Event()
    token = owner.reserve_finalizer()
    try:
        with observer.observe_owner(owner), observation.request_scope(9):
            first = owner.submit(lambda *_: release.wait(3))
            with pytest.raises(StorageBusy):
                owner.submit(lambda *_: None)
            final = owner.submit(lambda *_: 4, finalizer=token)
            release.set()
            await first.result()
            assert await final.result() == 4
        result = rows(recorder)
        assert any(r["phase"] == "queue" and r["outcome"] == "busy" for r in result)
        assert any(r["phase"] == "finalizer" and r["outcome"] == "ok" for r in result)
    finally:
        release.set()
        owner.release_finalizer(token, executor_finished=True, obligations_complete=True)
        await owner.close()


async def test_cancelled_queued_storage_command_has_a_row_without_executing(tmp_path):
    from tests.test_storage_owner import create

    from clytie.storage.owner import StorageCancelled

    owner, _ = await create(tmp_path)
    recorder = recording.Recorder()
    observer = observation.Observation(tmp_path, recorder, origin=0, timer=lambda: 1)
    release = threading.Event()
    called = []
    try:
        with observer.observe_owner(owner):
            first = owner.submit(lambda *_: release.wait(3))
            with observation.request_scope(11):
                cancelled = owner.submit(lambda *_: called.append(True))
            assert cancelled.cancel()
            release.set()
            await first.result()
            with pytest.raises(StorageCancelled):
                await cancelled.result()
        result = [row for row in rows(recorder) if row["request_id"] == 11]
        assert not called
        assert len(result) == 1 and result[0]["phase"] == "queue"
        assert result[0]["outcome"] == "cancelled"
    finally:
        release.set()
        await owner.close()


async def test_owner_evidence_cannot_close_while_real_command_is_still_running(tmp_path):
    from tests.test_storage_owner import create

    owner, _ = await create(tmp_path)
    recorder = recording.Recorder()
    observer = observation.Observation(tmp_path, recorder, origin=0, timer=lambda: 1)
    release = threading.Event()
    try:
        with pytest.raises(ValueError, match="owner_unsettled"):
            with observer.observe_owner(owner):
                command = owner.submit(lambda *_: release.wait(3))
        assert recorder.failed
    finally:
        release.set()
        try:
            await command.result()
        finally:
            await owner.close()


async def test_completed_owner_commands_are_not_retained_for_the_observation_window(tmp_path):
    from tests.test_storage_owner import create

    owner, _ = await create(tmp_path)
    recorder = recording.Recorder()
    observer = observation.Observation(tmp_path, recorder, origin=0, timer=lambda: 1)
    futures = []
    try:
        with observer.observe_owner(owner):
            for identifier in range(128):
                with observation.request_scope(identifier):
                    command = owner.submit(lambda *_: None)
                    futures.append(weakref.ref(command._future))
                    await command.result()
            del command
            # The real owner may hold its latest command while waiting for work.
            # A sentinel displaces it without closing the observation window.
            await owner.execute(lambda *_: None)
            loop = asyncio.get_running_loop()
            callbacks_ran = loop.create_future()
            loop.call_soon(callbacks_ran.set_result, None)
            await callbacks_ran
            gc.collect()
            assert all(reference() is None for reference in futures)
            assert len(rows(recorder)) == 2 * (128 + 1)
    finally:
        await owner.close()


@pytest.mark.parametrize("outcome", ["ok", "cancelled", "failed"])
async def test_exit_settles_done_future_before_its_callback_is_scheduled(tmp_path, outcome):
    loop = asyncio.get_running_loop()

    def submit(function, **kwargs):
        future = loop.create_future()
        if outcome == "cancelled":
            future.cancel()
        elif outcome == "failed":
            future.set_exception(ValueError("fictional_failure"))
        else:
            future.set_result(None)
        return SimpleNamespace(_future=future)

    owner = SimpleNamespace(submit=submit)
    recorder = recording.Recorder()
    observer = observation.Observation(tmp_path, recorder, origin=0, timer=lambda: 1)
    with observer.observe_owner(owner), observation.request_scope(17):
        command = owner.submit(lambda *_: None)
        assert command._future.done()
        assert rows(recorder) == []
    result = rows(recorder)
    assert len(result) == 1
    assert result[0]["request_id"] == 17
    assert result[0]["phase"] == "queue"
    assert result[0]["outcome"] == outcome
    callbacks_ran = loop.create_future()
    loop.call_soon(callbacks_ran.set_result, None)
    await callbacks_ran
    assert rows(recorder) == result


async def test_invalidated_recorder_still_releases_completed_future_references(tmp_path):
    loop = asyncio.get_running_loop()

    def submit(function, **kwargs):
        future = loop.create_future()
        future.set_result(None)
        return SimpleNamespace(_future=future)

    owner = SimpleNamespace(submit=submit)
    recorder = recording.Recorder()
    observer = observation.Observation(tmp_path, recorder, origin=0, timer=lambda: 1)
    with observer.observe_owner(owner):
        command = owner.submit(lambda *_: None)
        reference = weakref.ref(command._future)
        recorder.failed = True
        del command
        callbacks_ran = loop.create_future()
        loop.call_soon(callbacks_ran.set_result, None)
        await callbacks_ran
        gc.collect()
        assert reference() is None
        assert rows(recorder) == []
    assert recorder.failed


@pytest.mark.parametrize("cancel", [False, True])
def test_native_checks_keep_real_driver_close_and_stage_times(tmp_path, cancel):
    from clytie.backup import native_sqlite
    from clytie.backup.errors import BackupError
    from clytie.backup.io_client import io_checks

    recorder = recording.Recorder(format_version=4)
    ticks = iter(range(1000))
    observer = observation.Observation(tmp_path, recorder, origin=0, timer=lambda: next(ticks))
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if cancel and calls >= 2:
            raise BackupError("cancelled")

    with observer.install(), observation.request_scope(17):
        try:
            with native_sqlite.validation_stage():
                db = sqlite3.connect(tmp_path / "fictional.db")
                try:
                    with io_checks(check), native_sqlite.native_checks(db):
                        db.execute(
                            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<4096) SELECT sum(x) FROM n"
                        ).fetchall()
                finally:
                    assert not any(
                        row.get("event") == "connection_closed" for row in rows(recorder)
                    )
                    db.close()
                    assert not any(row.get("event") == "finished" for row in rows(recorder))
        except BackupError as exc:
            assert cancel and exc.code == "cancelled"
    events = [row for row in rows(recorder) if row["phase"] == "native_validation"]
    assert [row["event"] for row in events] == [
        "started",
        "connection_attached",
        *(["interrupt_accepted", "cancel_accepted"] if cancel else []),
        "driver_finished",
        "connection_closed",
        "finished",
    ]
    assert len({row["validation_id"] for row in events}) == 1
    assert all(row["request_id"] == 17 and row["internal_read_bytes"] is None for row in events)
    assert [row["at_s"] for row in events] == sorted(row["at_s"] for row in events)
    checked = recording.evidence.NativeEvents()
    for row in events:
        checked.add(row)
    checked.complete()


def test_native_stage_release_before_close_is_not_hidden_by_observer(tmp_path):
    from clytie.backup import native_sqlite

    recorder = recording.Recorder(format_version=4)
    observer = observation.Observation(tmp_path, recorder, origin=0, timer=lambda: 1)
    with observer.install():
        with native_sqlite.validation_stage():
            db = sqlite3.connect(tmp_path / "fictional.db")
            with native_sqlite.native_checks(db):
                db.execute("PRAGMA quick_check").fetchall()
        db.close()
    checked = recording.evidence.NativeEvents()
    with pytest.raises(recording.evidence.EvidenceError, match="native_release_unconfirmed"):
        for row in rows(recorder):
            if row["phase"] == "native_validation":
                checked.add(row)


def test_native_unclosed_connection_does_not_produce_completed_evidence(tmp_path):
    from clytie.backup import native_sqlite

    recorder = recording.Recorder(format_version=4)
    observer = observation.Observation(tmp_path, recorder, origin=0)
    db = None
    try:
        with pytest.raises(ValueError, match="native_unsettled"), observer.install():
            db = sqlite3.connect(tmp_path / "fictional.db")
            with native_sqlite.native_checks(db):
                db.execute("PRAGMA quick_check").fetchall()
        assert recorder.failed
        assert any(row.get("event") == "connection_close_unknown" for row in rows(recorder))
    finally:
        if db is not None:
            db.close()


@pytest.mark.parametrize("explicit_close", [False, True])
def test_legacy_sql_observation_preserves_unknown_close_and_requires_process_exit(
    tmp_path, monkeypatch, explicit_close
):
    native_tool = load_tool("measurement_native_observation")
    original_import = native_tool.importlib.import_module

    def old_product(name):
        if name == "clytie.backup.native_sqlite":
            raise ModuleNotFoundError(name=name)
        return original_import(name)

    monkeypatch.setattr(native_tool.importlib, "import_module", old_product)
    recorder = recording.Recorder(format_version=4)
    ticks = iter(range(1000))
    observer = observation.Observation(tmp_path, recorder, origin=0, timer=lambda: next(ticks))
    observer.sequence = 1 << 40
    db = None
    try:
        with observer.install():
            with sqlite3.connect(tmp_path / "fictional.db") as db:
                db.execute("CREATE TABLE fixture(value)")
                db.executemany("INSERT INTO fixture VALUES (?)", [(1,), (2,), (3,)])
                assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
                assert list(db.execute("SELECT value FROM fixture")) == [(1,), (2,), (3,)]
            # __exit__ preserved its original transaction-only behavior.
            assert not any(row.get("event") == "connection_closed" for row in rows(recorder))
            if explicit_close:
                db.close()
        events = [row for row in rows(recorder) if row["phase"] == "native_validation"]
        checked = recording.evidence.NativeEvents()
        for row in events:
            checked.add(row)
        if explicit_close:
            checked.complete()
        else:
            assert events[-1]["event"] == "connection_close_unknown"
            assert events[-1]["outcome"] == "unknown"
            with pytest.raises(
                recording.evidence.EvidenceError, match="native_release_unconfirmed"
            ):
                checked.complete()
            checked.complete(process_exit_s=1000)
        assert not any(row["event"] == "cancel_accepted" for row in events)
    finally:
        if db is not None:
            db.close()


def test_helpers_imported_during_observation_do_not_retain_previous_recorder(tmp_path, monkeypatch):
    import sys
    from types import ModuleType

    from clytie.backup import native_sqlite

    original = native_sqlite.native_checks
    first = recording.Recorder(format_version=4)
    with observation.Observation(tmp_path, first, origin=0).install():
        late = ModuleType("clytie.measurement_fictional")
        late.native_checks = native_sqlite.native_checks
        monkeypatch.setitem(sys.modules, late.__name__, late)
        assert late.native_checks is not original
    assert late.native_checks is original
    first.closed = True
    second = recording.Recorder(format_version=4)
    with observation.Observation(tmp_path, second, origin=0).install():
        db = sqlite3.connect(tmp_path / "fictional.db")
        try:
            with late.native_checks(db):
                db.execute("PRAGMA quick_check").fetchall()
        finally:
            db.close()
    assert not rows(first)
    assert any(row.get("event") == "finished" for row in rows(second))


def test_progress_failure_is_not_misreported_as_accepted_cancellation(tmp_path):
    from clytie.backup import native_sqlite
    from clytie.backup.errors import BackupError
    from clytie.backup.io_client import io_checks

    recorder = recording.Recorder(format_version=4)
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise BackupError("snapshot_view_expired")

    with observation.Observation(tmp_path, recorder, origin=0).install():
        db = sqlite3.connect(tmp_path / "fictional.db")
        try:
            with pytest.raises(BackupError, match="snapshot_view_expired"):
                with io_checks(check), native_sqlite.native_checks(db):
                    db.execute(
                        "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<4096) SELECT sum(x) FROM n"
                    ).fetchall()
        finally:
            db.close()
    events = rows(recorder)
    assert any(row.get("event") == "interrupt_accepted" for row in events)
    assert not any(row.get("event") == "cancel_accepted" for row in events)
    assert (
        next(row for row in events if row.get("event") == "driver_finished")["outcome"] == "failed"
    )


def test_nested_native_checks_bind_only_their_explicit_connections(tmp_path):
    from clytie.backup import native_sqlite

    recorder = recording.Recorder(format_version=4)
    with observation.Observation(tmp_path, recorder, origin=0).install():
        with native_sqlite.validation_stage():
            outer = sqlite3.connect(tmp_path / "outer.db")
            try:
                with native_sqlite.native_checks(outer):
                    inner = sqlite3.connect(tmp_path / "inner.db")
                    try:
                        with native_sqlite.native_checks(inner):
                            inner.execute("PRAGMA quick_check").fetchall()
                    finally:
                        inner.close()
                    outer.execute("PRAGMA quick_check").fetchall()
            finally:
                outer.close()
    checked = recording.evidence.NativeEvents()
    for row in rows(recorder):
        checked.add(row)
    checked.complete()
    assert len([row for row in rows(recorder) if row["event"] == "connection_attached"]) == 2


@pytest.mark.parametrize("source", ["worker", "sdk", "asyncio", "unrelated"])
@pytest.mark.parametrize("stage_exit", [False, True])
def test_native_checks_classify_actual_cancellation_types_without_message_matching(
    tmp_path, source, stage_exit
):
    from clytie.backup import native_sqlite
    from clytie.backup.worker import Cancelled as WorkerCancelled
    from clytie.sdk.cancellation import Cancelled as SdkCancelled

    class Cancelled(Exception):
        pass

    exception = {
        "worker": WorkerCancelled,
        "sdk": SdkCancelled,
        "asyncio": asyncio.CancelledError,
        "unrelated": Cancelled,
    }[source]
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls >= 2 and not stage_exit:
            raise exception("cancelled")

    from clytie.backup.io_client import io_checks

    recorder = recording.Recorder(format_version=4)
    with observation.Observation(tmp_path, recorder, origin=0).install():
        with pytest.raises(exception):
            with native_sqlite.validation_stage():
                db = sqlite3.connect(tmp_path / "fictional.db")
                try:
                    with io_checks(check), native_sqlite.native_checks(db):
                        db.execute(
                            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<4096) SELECT sum(x) FROM n"
                        ).fetchall()
                finally:
                    db.close()
                if stage_exit:
                    raise exception("cancelled")
    events = rows(recorder)
    expected = "failed" if source == "unrelated" else "cancelled"
    assert next(row for row in events if row["event"] == "driver_finished")["outcome"] == (
        "ok" if stage_exit else expected
    )
    assert next(row for row in events if row["event"] == "finished")["outcome"] == expected
    accepted = [row for row in events if row["event"] == "cancel_accepted"]
    assert len(accepted) == (0 if source == "unrelated" or stage_exit else 1)
    if accepted:
        interrupt = next(row for row in events if row["event"] == "interrupt_accepted")
        assert accepted[0]["at_s"] == interrupt["at_s"]
