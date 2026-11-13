import json
import sqlite3

import pytest
from tests.test_measurement_evidence import load_tool

from clytie.backup.capture import capture_group
from clytie.backup.coordination import CaptureCoordinator
from clytie.sdk.testing import MockClock

load_tool("measurement_recording")
views = load_tool("measurement_views")


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("first_id", [1, 2])
async def test_real_view_and_thread_release_are_recorded_after_actual_completion(
    tmp_path, cancel, first_id
):
    path = tmp_path / "fictional.db"
    source = sqlite3.connect(path)
    source.execute("PRAGMA journal_mode=WAL")
    source.execute("CREATE TABLE fixture(value)")
    source.execute("INSERT INTO fixture VALUES ('fictional')")
    source.commit()
    recorder = views.Recorder()
    observer = views.Views(recorder, now=lambda: 0, first_id=first_id)
    clock = MockClock()
    try:
        with observer.install():
            group = await capture_group(
                {"memory": path},
                CaptureCoordinator(tmp_path / "capture.lock", clock),
                epoch="fictional",
            )
            assert observer.pending == 1
            with pytest.raises(ValueError, match="release_unconfirmed"):
                observer.verify_released()
            if cancel:
                group._worker.cancelled.set()
                with pytest.raises(ValueError, match="cancelled"):
                    await group.materialize(tmp_path / "copy")
            else:
                await group.materialize(tmp_path / "copy")
            assert observer.pending == 0
            observer.verify_released()
            observer.verify_released()
        events = [json.loads(row) for row in recorder.rows["release-events.jsonl"].splitlines()]
        assert [r["event"] for r in events if r["resource"] == "view"] == ["acquired", "released"]
        assert sum(r["event"] == "exit_verified" for r in events) == 1
        assert {row["id"] for row in events} == {first_id}
        assert all(not worker.thread.is_alive() for worker in observer.workers)
    finally:
        source.close()
