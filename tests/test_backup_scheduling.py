import importlib.util
from datetime import datetime, timezone

import pytest

from clytie.sdk.testing import MockClock


def scheduler(tmp_path, clock=None, **config):
    assert importlib.util.find_spec("clytie.backup.scheduling") is not None
    from clytie.backup.scheduling import BackupScheduler

    clock = clock or MockClock(datetime(2030, 1, 1, 0, tzinfo=timezone.utc))
    base = {"schedule": {"at": "23:59", "days": [], "catch_up": True}, **config}
    return BackupScheduler(tmp_path, clock=clock, timezone="UTC", config=base), clock


def test_first_dirty_deadline_never_slides_and_only_important_changes(tmp_path):
    s, c = scheduler(tmp_path)
    assert not s.mark_dirty("companion_a", 1, "retrieval")
    s.mark_dirty("companion_a", 2, "fact")
    c.advance(299)
    s.mark_dirty("companion_a", 3, "revision")
    assert s.poll() is None
    c.advance(1)
    request = s.poll()
    assert request.scope == "memory"
    assert request.requested_versions == {"companion_a": 3}
    assert s.poll() is None
    s.finish(request.operation_id, creation="complete", covered_versions={"companion_a": 3})
    assert s.poll() is None


def test_instance_coverage_does_not_clear_newer_dirty_or_partial(tmp_path):
    s, c = scheduler(tmp_path)
    s.mark_dirty("companion_a", 1, "import")
    s.manual("manual_a", "instance")
    request = s.poll()
    s.mark_dirty("companion_a", 2, "retraction")
    s.finish(request.operation_id, creation="complete", covered_versions={"companion_a": 1})
    c.advance(300)
    memory = s.poll()
    assert memory.requested_versions == {"companion_a": 2}
    s.finish(memory.operation_id, creation="partial", covered_versions={})
    assert s.status()["dirty"]["companion_a"]["revision"] == 2


def test_pending_coalesces_and_restart_does_not_replay_active(tmp_path):
    s, c = scheduler(tmp_path)
    s.mark_dirty("companion_a", 1, "fact")
    c.advance(300)
    active = s.poll()
    s.mark_dirty("companion_a", 2, "fact")
    again, _ = scheduler(tmp_path, c)
    assert again.poll() is None
    again.recover_interrupted(active.operation_id)
    followup = again.poll()
    assert followup.operation_id != active.operation_id
    assert followup.requested_versions == {"companion_a": 2}


def test_manual_identity_and_maintenance_and_disabled_auto(tmp_path):
    s, c = scheduler(tmp_path, enabled=False)
    s.mark_dirty("companion_a", 1, "fact")
    c.advance(300)
    assert s.poll() is None
    s.manual("manual_a", "memory", "companion_a")
    s.manual("manual_a", "memory", "companion_a")
    assert s.poll(maintenance=True) is None
    request = s.poll()
    assert request.manual and request.operation_id == "manual_a"
    s.finish(request.operation_id, creation="complete", covered_versions={"companion_a": 1})
    s.manual("manual_a", "memory", "companion_a")
    assert s.poll() is None


def test_shutdown_is_once_and_only_when_pending(tmp_path):
    s, c = scheduler(tmp_path)
    assert not s.shutdown("stop_empty")
    s.mark_dirty("companion_a", 1, "fact")
    assert s.shutdown("stop_a")
    assert not s.shutdown("stop_a")
    request = s.poll()
    assert request.trigger == "shutdown"
    s.finish(request.operation_id, creation="complete", covered_versions={"companion_a": 1})
    assert not s.shutdown("stop_a")


def test_daily_weekly_and_restart_catchup_coalesce(tmp_path):
    c = MockClock(datetime(2030, 1, 1, 2, 59, tzinfo=timezone.utc))
    s, _ = scheduler(tmp_path, c, schedule={"at": "03:00", "days": [], "catch_up": True})
    assert s.poll() is None
    c.advance(60)
    req = s.poll()
    assert req.scope == "instance" and req.trigger == "schedule"
    s.finish(req.operation_id, creation="complete")
    assert s.poll() is None
    c.advance(5 * 24 * 3600)
    restart, _ = scheduler(tmp_path, c, schedule={"at": "03:00", "days": [], "catch_up": True})
    latest = restart.poll()
    restart.finish(latest.operation_id, creation="complete")
    assert restart.poll() is None
    weekly_path = tmp_path / "weekly"
    weekly, _ = scheduler(weekly_path, c, schedule={"at": "03:00", "days": [1], "catch_up": True})
    assert weekly.poll() is None  # Sunday
    c.advance(24 * 3600)
    assert weekly.poll().scope == "instance"


def test_disabled_catchup_skips_offline_due_but_runs_future(tmp_path):
    c = MockClock(datetime(2030, 1, 1, 2, tzinfo=timezone.utc))
    config = {"at": "03:00", "days": [], "catch_up": False}
    s, _ = scheduler(tmp_path, c, schedule=config)
    assert s.poll() is None
    c.advance(2 * 3600)
    restart, _ = scheduler(tmp_path, c, schedule=config)
    assert restart.poll() is None
    c.advance(23 * 3600)
    assert restart.poll().scope == "instance"


def test_dst_gap_and_fold_use_local_date_once(tmp_path):
    from clytie.backup.scheduling import BackupScheduler

    c = MockClock(datetime(2030, 3, 10, 6, 59, tzinfo=timezone.utc))
    s = BackupScheduler(
        tmp_path, clock=c, timezone="America/New_York", config={"schedule": {"at": "02:30"}}
    )
    assert s.poll() is None
    c.advance(60)
    first = s.poll()
    assert first is not None  # 03:00 is first valid time after the spring gap
    s.finish(first.operation_id, creation="complete")
    assert s.poll() is None
    c = MockClock(datetime(2030, 11, 3, 5, 30, tzinfo=timezone.utc))
    fold = BackupScheduler(
        tmp_path / "fold",
        clock=c,
        timezone="America/New_York",
        config={"schedule": {"at": "01:30"}},
    )
    request = fold.poll()
    fold.finish(request.operation_id, creation="complete")
    c.advance(3600)
    assert fold.poll() is None


def test_scheduler_uses_os_lock_and_preserves_previous_state(tmp_path):
    import subprocess
    import sys

    import pytest

    from clytie.plugin_host.locking import OperationBusy

    s, _ = scheduler(tmp_path)
    s.mark_dirty("companion_a", 1, "fact")
    script = """
import sys
from clytie.plugin_host.locking import file_lock
with file_lock(sys.argv[1], {'operation': 'test'}):
    print('locked', flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(s.lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(OperationBusy):
            s.mark_dirty("companion_a", 2, "revision")
    finally:
        process.communicate("\n", timeout=10)
    assert s.status()["dirty"]["companion_a"]["revision"] == 1


def test_successful_instance_cancels_covered_pending_memory_and_duplicate_revision(tmp_path):
    s, c = scheduler(tmp_path)
    s.mark_dirty("companion_a", 1, "fact")
    s.manual("manual_a", "instance")
    c.advance(300)
    instance = s.poll()
    assert instance.scope == "instance"
    s.finish(instance.operation_id, creation="complete", covered_versions={"companion_a": 1})
    assert not s.mark_dirty("companion_a", 1, "fact")
    assert s.poll() is None


def test_shutdown_promotes_existing_instance_pending_without_duplicate(tmp_path):
    s, c = scheduler(tmp_path, schedule={"at": "00:01"})
    s.manual("manual_a", "memory", "companion_a")
    manual = s.poll()
    c.advance(60)
    assert s.poll() is None
    assert s.shutdown("stop_a")
    s.finish(manual.operation_id, creation="complete")
    request = s.poll()
    assert request.trigger == "shutdown"
    s.finish(request.operation_id, creation="complete")
    assert s.poll() is None


def test_partial_and_unverified_cannot_clear_dirty(tmp_path):
    s, c = scheduler(tmp_path)
    s.mark_dirty("companion_a", 1, "fact")
    s.manual("manual_a", "instance")
    request = s.poll()
    s.finish(request.operation_id, creation="partial", covered_versions={"companion_a": 1})
    assert s.status()["dirty"]["companion_a"]["revision"] == 1
    s.manual("manual_b", "instance")
    request = s.poll()
    s.finish(
        request.operation_id,
        creation="complete",
        covered_versions={"companion_a": 1},
        verified=False,
    )
    assert s.status()["dirty"]["companion_a"]["revision"] == 1


def test_unknown_result_and_conflicting_manual_identity_do_not_mutate(tmp_path):
    import pytest

    s, c = scheduler(tmp_path)
    s.manual("manual_a", "instance")
    before = s.status()
    with pytest.raises(ValueError, match="identity_conflict"):
        s.manual("manual_a", "memory", "companion_a")
    assert s.status() == before
    request = s.poll()
    before = s.status()
    with pytest.raises(ValueError, match="not_active"):
        s.finish("unknown_id", creation="complete")
    assert s.status() == before
    assert request.operation_id == "manual_a"


def test_maintenance_does_not_consume_due_schedule_or_start_requests(tmp_path):
    s, c = scheduler(tmp_path, schedule={"at": "00:01"})
    c.advance(60)
    assert s.poll(maintenance=True) is None
    assert s.poll(ready=False) is None
    assert s.poll().scope == "instance"


def test_corrupt_scheduler_state_is_not_replaced(tmp_path):
    import pytest

    s, c = scheduler(tmp_path)
    s.path.parent.mkdir(parents=True)
    s.path.write_text('{"schema_version": 999}')
    with pytest.raises(ValueError, match="incompatible"):
        s.poll()
    assert s.path.read_text() == '{"schema_version": 999}'


def test_result_tracks_verification_independently_and_disabled_memory_stays_pending(tmp_path):
    s, c = scheduler(tmp_path)
    s.mark_dirty("companion_a", 1, "fact")
    s.manual("manual_a", "instance")
    c.advance(300)
    active = s.poll()
    s.finish(active.operation_id, creation="complete", verified=False)
    state = s.status()
    assert state["jobs"]["manual:manual_a"]["verification"] == "failed"
    assert state["jobs"]["manual:manual_a"]["creation"] == "complete"
    disabled, _ = scheduler(tmp_path, c, memory={"enabled": False})
    assert disabled.poll() is None


def test_persistent_four_attempts_backoff_from_confirmed_failure_and_exhaustion(tmp_path):
    s, c = scheduler(tmp_path)
    s.mark_dirty("companion_a", 1, "fact")
    c.advance(300)
    request = s.poll()
    trigger = request.trigger_id
    for attempt, delay in enumerate([600, 1800, 3600], start=1):
        assert request.attempt == attempt
        s.finish(request.operation_id, creation="failed", verified=False)
        due = s.status()["jobs"][trigger]["due"]
        s.finish(request.operation_id, creation="failed", verified=False)
        assert s.status()["jobs"][trigger]["due"] == due
        s, _ = scheduler(tmp_path, c)
        c.advance(delay - 1)
        assert s.poll() is None
        c.advance(1)
        request = s.poll()
        assert request.trigger_id == trigger
    assert request.attempt == 4
    s.finish(request.operation_id, creation="failed", verified=False)
    assert s.status()["jobs"][trigger]["status"] == "exhausted"
    c.advance(3600)
    assert s.poll() is None
    assert len(s.status()["jobs"][trigger]["attempts"]) == 4
    s.mark_dirty("companion_a", 2, "revision")
    assert s.poll().trigger_id != trigger


def test_cancel_unknown_committed_and_success_never_retry(tmp_path):
    for outcome in ["cancelled", "unknown", "committed", "success"]:
        s, c = scheduler(tmp_path / outcome)
        s.mark_dirty("companion_a", 1, "fact")
        c.advance(300)
        request = s.poll()
        if outcome == "committed":
            s.finish(request.operation_id, creation="failed", verified=False, committed=True)
        elif outcome == "success":
            s.finish(request.operation_id, creation="complete", covered_versions={"companion_a": 1})
        else:
            s.finish(request.operation_id, creation=outcome, verified=False)
        c.advance(600)
        assert s.poll() is None
        if outcome == "unknown":
            assert s.status()["active"] == request.trigger_id
            s.finish(request.operation_id, creation="complete", covered_versions={"companion_a": 1})
            assert s.poll() is None


def test_retry_after_downtime_runs_only_one_current_attempt(tmp_path):
    s, c = scheduler(tmp_path)
    s.mark_dirty("companion_a", 1, "fact")
    c.advance(300)
    request = s.poll()
    s.finish(request.operation_id, creation="failed", verified=False)
    c.advance(4 * 3600)
    restart, _ = scheduler(tmp_path, c)
    retry = restart.poll()
    assert retry.attempt == 2
    assert restart.poll() is None
    restart.finish(retry.operation_id, creation="failed", verified=False)
    assert restart.poll() is None
    c.advance(1800)
    assert restart.poll().attempt == 3


def test_failed_manual_does_not_consume_independent_memory_trigger(tmp_path):
    s, c = scheduler(tmp_path)
    s.mark_dirty("companion_a", 1, "fact")
    s.manual("manual_a", "instance")
    request = s.poll()
    s.finish(request.operation_id, creation="failed", verified=False)
    c.advance(300)
    assert s.poll().trigger == "memory"


def test_cancelled_retry_preserves_history_and_does_not_revive_on_restart(tmp_path):
    s, c = scheduler(tmp_path)
    s.mark_dirty("companion_a", 1, "fact")
    c.advance(300)
    first = s.poll()
    s.finish(first.operation_id, creation="failed", verified=False)
    c.advance(600)
    second = s.poll()
    s.finish(second.operation_id, creation="cancelled", verified=False)
    c.advance(3600)
    restart, _ = scheduler(tmp_path, c)
    assert restart.poll() is None
    job = restart.status()["jobs"][first.trigger_id]
    assert job["attempt"] == 2
    assert [attempt["result"]["creation"] for attempt in job["attempts"]] == ["failed", "cancelled"]


def test_shutdown_never_promotes_future_retry_or_changes_budget(tmp_path):
    s, c = scheduler(tmp_path, schedule={"at": "00:01"})
    c.advance(60)
    failed = s.poll()
    s.finish(failed.operation_id, creation="failed", verified=False)
    old = s.status()["jobs"][failed.trigger_id]
    assert not s.shutdown("stop_no_dirty")
    s.mark_dirty("companion_a", 1, "fact")
    assert s.shutdown("stop_dirty")
    assert not s.shutdown("stop_dirty")
    request = s.poll()
    assert request.trigger == "shutdown"
    assert request.trigger_id != failed.trigger_id
    retained = s.status()["jobs"][failed.trigger_id]
    assert retained["due"] == old["due"] and retained["attempt"] == old["attempt"]
    assert retained["trigger"] == "schedule"
    s.finish(request.operation_id, creation="cancelled", verified=False)
    assert s.poll() is None


@pytest.mark.parametrize("successful_attempt", [1, 2, 3, 4])
def test_success_at_each_attempt_ends_persistent_retry(tmp_path, successful_attempt):
    s, c = scheduler(tmp_path)
    s.mark_dirty("companion_a", 1, "fact")
    c.advance(300)
    request = s.poll()
    for delay in (600, 1800, 3600)[: successful_attempt - 1]:
        # Execution and verification time do not count towards the next delay.
        c.advance(17)
        s.finish(request.operation_id, creation="failed", verified=False)
        c.advance(delay - 1)
        assert s.poll() is None
        c.advance(1)
        request = s.poll()
    assert request.attempt == successful_attempt
    s.finish(request.operation_id, creation="complete", covered_versions={"companion_a": 1})
    restarted, _ = scheduler(tmp_path, c)
    c.advance(3600)
    assert restarted.poll() is None
    job = restarted.status()["jobs"][request.trigger_id]
    assert job["status"] == "complete"
    assert len(job["attempts"]) == successful_attempt
