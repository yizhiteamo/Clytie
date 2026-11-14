import asyncio
import hashlib
import json
import sqlite3

import pytest
from tests.measurement_tools import load

from clytie.backup.capture import capture_group
from clytie.backup.coordination import CaptureCoordinator
from clytie.backup.io_client import IoClient, io_scope
from clytie.backup.journal import BackupJournal
from clytie.backup.worker import Cancelled
from clytie.backup.workers import BackupWorkers
from clytie.sdk.testing import MockClock

cancel = load("measurement_cancel")
PlannedCancel, validate_record = cancel.PlannedCancel, cancel.validate_record
requests = load("measurement_load").requests
Observation = load("measurement_observation").Observation
Recorder = load("measurement_recording").Recorder
Views = load("measurement_views").Views


def rows(recorder):
    return [json.loads(line) for line in recorder.rows["storage-phases.jsonl"].splitlines()]


def setup(tmp_path, scale):
    clock = MockClock()
    recorder = Recorder(format_version=6)
    observer = Observation(tmp_path, recorder, origin=0, process_sequence=0)
    workers = BackupWorkers(tmp_path, clock)
    operation = "e" * 32
    journal, _ = BackupJournal.accept(tmp_path, clock, operation, "create", {"scope": "instance"})
    control = []
    workers.controls[operation] = lambda: control.append("native_control")
    plan = PlannedCancel(observer, workers, operation, scale=scale)
    return clock, recorder, observer, workers, journal, control, plan


async def test_real_fixed_view_first_copy_chunk_requests_native_cancel(tmp_path):
    clock, recorder, observer, workers, journal, control, plan = setup(tmp_path, "large_database")
    path = tmp_path / "memory.db"
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE fictional(payload BLOB)")
    db.execute("INSERT INTO fictional VALUES (zeroblob(?))", (800 * 1000,))
    db.commit()
    loop = asyncio.get_running_loop()
    calls = []

    async def transport(value):
        calls.append(value)
        return {}

    def request(value, **kwargs):
        return asyncio.run_coroutine_threadsafe(transport(value), loop).result()

    def check():
        if workers.query(journal.value["id"])["cancel_requested"]:
            raise Cancelled()

    views = Views(recorder, now=observer.now)
    business_results, schedule_time = [], [0.0]

    async def business_wait(scheduled):
        await asyncio.shield(plan.started)
        schedule_time[0] = scheduled

    async def business(index):
        business_results.append(index)

    business_task = asyncio.create_task(
        requests(
            recorder,
            count=7,
            interval=0.025,
            start=0.0,
            origin=0.0,
            now=lambda: schedule_time[0],
            wait=business_wait,
            operation=business,
        )
    )
    try:
        with observer.install(), views.install(), plan.install(), io_scope(IoClient(request)):
            view = await capture_group(
                {"memory": path},
                CaptureCoordinator(tmp_path / "capture.lock", clock),
                epoch="fictional",
            )
            with pytest.raises(Cancelled):
                await view.materialize(tmp_path / "copy", pages=128, check=check)
            await plan.settle()
            assert await business_task
            assert view.closed and not view.resource_pending
            views.verify_released()
        assert control == ["native_control"]
        assert workers.query(journal.value["id"])["cancel_requested"] is True
        cancel = [row for row in rows(recorder) if row["phase"] == "planned_cancel"]
        assert [row["event"] for row in cancel] == ["triggered", "requested", "accepted"]
        source = next(row for row in rows(recorder) if row["id"] == cancel[0]["source_row_id"])
        assert source["phase"] == "sqlite_batch" and source["copied_pages"] > 0
        assert cancel[0]["view_id"] == views.workers[view._worker]
        assert calls[-1]["action"] == "release"
        assert not recorder.failed
        assert business_results == list(range(7))
        business_rows = [json.loads(line) for line in recorder.rows["requests.jsonl"].splitlines()]
        assert [row["scheduled_s"] for row in business_rows] == [i * 0.025 for i in range(7)]
        assert all(row["outcome"] == "ok" for row in business_rows)
    finally:
        db.close()


async def test_no_trigger_and_terminal_race_are_not_expected_cancel(tmp_path):
    *_, plan = setup(tmp_path, "large_database")
    await plan.settle()
    assert plan.terminal({"phase": "committed", "cancel_requested": False}) is False


async def test_real_asset_first_actual_write_not_other_file_io(tmp_path):
    from clytie.assets import AssetStore
    from clytie.backup import io_client
    from clytie.sdk.types import AssetRef

    clock, recorder, observer, workers, journal, control, plan = setup(tmp_path, "large_asset")
    store = AssetStore(tmp_path, clock)
    content = b"fictional" * (100 * 1000)
    relative = "companions/companion_a/assets/fictional.bin"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    ref = AssetRef(
        asset_id=hashlib.sha256(content).hexdigest(),
        size=len(content),
        media_type="application/octet-stream",
        origin="fictional",
    )
    store.register_private("companion_a", ref, relative)
    resolved = store.resolve(store.snapshot(), "companion_a", ref.asset_id)
    loop = asyncio.get_running_loop()

    async def transport(value):
        return {}

    def request(value, **kwargs):
        return asyncio.run_coroutine_threadsafe(transport(value), loop).result()

    def check():
        if workers.query(journal.value["id"])["cancel_requested"]:
            raise Cancelled()

    def copying():
        client = IoClient(request)
        with io_scope(client), io_client.io_checks(check), client.stage():
            with (tmp_path / "unrelated").open("wb") as stream:
                io_client.io_write(stream, b"fictional")
            assert plan.source is None
            store.copy_resolved(
                resolved,
                tmp_path / "asset-copy",
                check=check,
                reader=io_client.io_read,
                writer=io_client.io_write,
            )

    with observer.install(), plan.install():
        with pytest.raises(Cancelled):
            await asyncio.to_thread(copying)
        await plan.settle()
    cancel = [row for row in rows(recorder) if row["phase"] == "planned_cancel"]
    assert [row["event"] for row in cancel] == ["triggered", "requested", "accepted"]
    source = next(row for row in rows(recorder) if row["id"] == cancel[0]["source_row_id"])
    assert source["phase"] == "file_batch" and source["direction"] == "write"
    assert source["actual_bytes"] == 256 * 1024
    assert cancel[0]["view_id"] is None
    assert control == ["native_control"]
    assert not recorder.failed


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
async def test_old_formats_do_not_enable_plan_cancel(tmp_path, version):
    recorder = Recorder(format_version=version)
    observer = Observation(tmp_path, recorder, origin=0, process_sequence=0)
    with pytest.raises(ValueError, match="measurement_cancel_format_invalid"):
        PlannedCancel(observer, None, "e" * 32, scale="large_asset")


def test_cancel_schema_rejects_arbitrary_fields():
    with pytest.raises(ValueError):
        validate_record({"phase": "planned_cancel", "path": "fictional"})
