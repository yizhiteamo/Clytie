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
