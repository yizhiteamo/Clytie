import json
import os
import sqlite3
import threading

import pytest
from tests.test_measurement_evidence import load_tool

observation = load_tool("measurement_observation")
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
