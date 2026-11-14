from datetime import datetime, timedelta, timezone

from clytie.backup.retention import BackupRecord, RetentionConfig, plan_retention

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def record(i, **kwargs):
    values = dict(
        backup_id=str(i),
        path=f"regular/{i}.clytie-backup",
        scope="instance",
        group="instance_a",
        completed_at=NOW - timedelta(days=20 - i),
        size=10,
    )
    values.update(kwargs)
    return BackupRecord(**values)


def test_capacity_rolls_old_total_only_and_protects_latest_three():
    records = [record(i) for i in range(6)]
    config = RetentionConfig(target_bytes=25)
    first = plan_retention(records, config, now=NOW, started_bytes=25, new_backup_id="5")
    assert first.delete == ()
    next_plan = plan_retention(records, config, now=NOW, started_bytes=50, new_backup_id="5")
    assert next_plan.delete == tuple(r.path for r in records[:3])
    assert next_plan.old_bytes_remaining == 20


def test_manual_and_references_and_each_group_protected():
    records = [record(i, referenced=i == 0, manual=i == 1) for i in range(6)]
    records += [record(20, scope="memory", group="companion_a")]
    plan = plan_retention(
        records, RetentionConfig(target_bytes=1), now=NOW, started_bytes=60, new_backup_id="5"
    )
    assert plan.delete == (records[2].path,)
    assert plan.status == "retention_protected"


def test_partial_does_not_rotate_complete():
    records = [record(i) for i in range(6)] + [
        record(i + 10, completeness="partial") for i in range(4)
    ]
    plan = plan_retention(
        records, RetentionConfig(target_bytes=1), now=NOW, started_bytes=90, new_backup_id="13"
    )
    assert plan.delete == (records[6].path,)


def test_failed_new_package_never_cleans():
    plan = plan_retention(
        [record(0)],
        RetentionConfig(target_bytes=1),
        now=NOW,
        started_bytes=10,
        new_backup_id="missing",
    )
    assert plan.delete == ()


def test_memory_frequency_union_daily_and_deterministic_order():
    records = [
        record(i, scope="memory", group="companion_a", completed_at=NOW - timedelta(minutes=30 - i))
        for i in range(20)
    ]
    plan = plan_retention(
        list(reversed(records)),
        RetentionConfig(target_bytes=None),
        now=NOW,
        started_bytes=190,
        new_backup_id="19",
    )
    assert plan.delete == tuple(r.path for r in records[:8])


def test_mirror_retention_only_owned_complete_and_references():
    from clytie.backup.retention import plan_mirror_retention

    records = [record(i, completed_at=NOW - timedelta(minutes=30 - i)) for i in range(12)]
    records[0] = record(0, owned=False)
    records[1] = record(1, referenced=True)
    plan = plan_mirror_retention(records, now=NOW)
    assert plan.delete == tuple(r.path for r in records[2:5])
