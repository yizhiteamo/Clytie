"""解除恢复阻止的明确版本选择、异常日志及提交崩溃。"""

import json

import pytest

from clytie.plugin_host import PlanStale
from clytie.plugin_host.snapshots import atomic_write, canonical

from .test_host_journal import Crash, interrupted


def replace_record(h, value):
    versions = h._versions("companion_a")
    if value is None:
        versions.pop("stateful", None)
    else:
        versions["stateful"] = value
    versions["other"] = {"plugin_version": "4.0.0", "data_version": 4}
    atomic_write(h._versions_path("companion_a"), canonical(versions))


@pytest.mark.parametrize("value", [None, {"plugin_version": "3.0.0", "data_version": 3}])
async def test_retry_restores_missing_or_abnormal_version_without_touching_other_items(
    tmp_path, value
):
    h, _, old, record = await interrupted(tmp_path)
    replace_record(h, value)
    plan = h.plan_recovery("companion_a", "stateful", "retry_restore")
    assert plan.version_before == value
    assert plan.version_after == record["previous_version"]
    with pytest.raises(TypeError):
        plan.version_after["data_version"] = 9
    assert "write_version_record" in plan.steps
    await h.apply_recovery(plan.plan_id)
    assert await old.storage.get("value") == "fictional-original"
    assert h._version("companion_a", "stateful") == record["previous_version"]
    assert h._version("companion_a", "other")["data_version"] == 4


@pytest.mark.parametrize("choice", ["old", "target"])
async def test_accept_abnormal_record_requires_explicit_version_choice(tmp_path, choice):
    h, _, old, record = await interrupted(tmp_path)
    replace_record(h, None)
    with pytest.raises(ValueError, match="explicit"):
        h.plan_recovery("companion_a", "stateful", "accept_current")
    plan = h.plan_recovery("companion_a", "stateful", "accept_current", version_choice=choice)
    assert plan.version_choice == choice and plan.warning
    await h.apply_recovery(plan.plan_id)
    assert await old.storage.get("value") == "fictional-partial-migration"
    assert h._version("companion_a", "stateful")["data_version"] == (1 if choice == "old" else 2)
    assert h._load_status("companion_a", h.manifests["stateful"])[0] == (
        "pending_migration" if choice == "old" else None
    )


@pytest.mark.parametrize("choice", [None, 0, 3, True, "1"])
async def test_damaged_journal_rejects_unspecified_or_out_of_range_version(tmp_path, choice):
    h, _, _, _ = await interrupted(tmp_path)
    h.journals.path("companion_a", "stateful").write_bytes(b"fictional-broken-json")
    with pytest.raises(ValueError, match="unavailable"):
        h.plan_recovery("companion_a", "stateful", "retry_restore")
    with pytest.raises(ValueError, match="explicit"):
        h.plan_recovery("companion_a", "stateful", "accept_current", version_choice=choice)


async def test_damaged_journal_accepts_explicit_integer_with_warning(tmp_path):
    h, _, _, _ = await interrupted(tmp_path)
    h.journals.path("companion_a", "stateful").write_bytes(b"fictional-broken-json")
    plan = h.plan_recovery("companion_a", "stateful", "accept_current", version_choice=1)
    assert "无法核对" in plan.warning
    await h.apply_recovery(plan.plan_id)
    assert h._version("companion_a", "stateful")["data_version"] == 1
    assert not h.journals.path("companion_a", "stateful").exists()


@pytest.mark.parametrize(
    "operation,choice",
    [("retry_restore", None), ("accept_current", "old"), ("accept_current", "target")],
)
async def test_version_written_before_log_removal_can_be_retried_idempotently(
    tmp_path, monkeypatch, operation, choice
):
    h, _, old, _ = await interrupted(tmp_path)
    replace_record(h, None)
    plan = h.plan_recovery("companion_a", "stateful", operation, version_choice=choice)
    clear = h.journals.clear

    def crash(*args):
        raise Crash()

    monkeypatch.setattr(h.journals, "clear", crash)
    with pytest.raises(Crash):
        await h.apply_recovery(plan.plan_id)
    expected = h._versions_path("companion_a").read_bytes()
    state = old.storage.path.joinpath(".values.json").read_bytes()
    assert h.journals.read("companion_a", "stateful")["phase"] == "restoring"
    other_operation = "accept_current" if operation == "retry_restore" else "retry_restore"
    with pytest.raises(ValueError, match="same confirmed"):
        h.plan_recovery("companion_a", "stateful", other_operation)
    if operation == "accept_current":
        with pytest.raises(ValueError, match="same confirmed"):
            h.plan_recovery(
                "companion_a",
                "stateful",
                operation,
                version_choice="target" if choice == "old" else "old",
            )
    monkeypatch.setattr(h.journals, "clear", clear)
    retry = h.plan_recovery("companion_a", "stateful", operation, version_choice=choice)
    await h.apply_recovery(retry.plan_id)
    assert h._versions_path("companion_a").read_bytes() == expected
    assert old.storage.path.joinpath(".values.json").read_bytes() == state
    assert not h.journals.path("companion_a", "stateful").exists()


async def test_version_edit_invalidates_recovery_plan(tmp_path):
    h, _, _, _ = await interrupted(tmp_path)
    plan = h.plan_recovery("companion_a", "stateful", "retry_restore")
    replace_record(h, None)
    with pytest.raises(PlanStale):
        await h.apply_recovery(plan.plan_id)


async def test_controlled_restore_failure_allows_new_choice_and_keeps_audit(tmp_path, monkeypatch):
    from clytie.plugin_host.recovery import RecoveryError

    h, _, old, record = await interrupted(tmp_path)
    replace_record(h, None)
    plan = h.plan_recovery("companion_a", "stateful", "retry_restore")
    audit = h.journals.audit

    def corrupt_after_confirmation(record, action):
        audit(record, action)
        if action == "retry_restore_confirmed":
            (tmp_path / record["snapshot"]).write_bytes(b"fictional-corrupted-snapshot")

    monkeypatch.setattr(h.journals, "audit", corrupt_after_confirmation)
    with pytest.raises(RecoveryError):
        await h.apply_recovery(plan.plan_id)
    failed = h.journals.read("companion_a", "stateful")
    assert failed["resolution"]["status"] == "failed" and failed["error"]
    assert h._version("companion_a", "stateful") is None
    replacement = h.plan_recovery("companion_a", "stateful", "accept_current", version_choice="old")
    await h.apply_recovery(replacement.plan_id)
    assert await old.storage.get("value") == "fictional-partial-migration"
    assert h._version("companion_a", "stateful") == record["previous_version"]
    rows = [
        json.loads(line)
        for line in h.journals.path("companion_a", "stateful")
        .with_suffix(".audit.jsonl")
        .read_text()
        .splitlines()
    ]
    actions = [row["action"] for row in rows]
    assert actions == [
        "retry_restore_confirmed",
        "resolution_failed",
        "resolution_previous_choice",
        "accept_current_confirmed",
        "current_directory_accepted",
    ]
    assert rows[0]["resolution"]["operation"] == "retry_restore"
    assert rows[1]["resolution"]["status"] == "failed"
    assert rows[3]["resolution"]["choice"] == "old"


async def test_crash_immediately_after_version_replace_forbids_changed_choice(
    tmp_path, monkeypatch
):
    h, _, _, _ = await interrupted(tmp_path)
    replace_record(h, None)
    plan = h.plan_recovery("companion_a", "stateful", "accept_current", version_choice="target")
    write = h.journals.version_writer

    def crash(*args):
        write(*args)
        raise Crash()

    monkeypatch.setattr(h.journals, "version_writer", crash)
    with pytest.raises(Crash):
        await h.apply_recovery(plan.plan_id)
    assert h.journals.read("companion_a", "stateful")["resolution"]["status"] == "writing"
    with pytest.raises(ValueError, match="same confirmed"):
        h.plan_recovery("companion_a", "stateful", "accept_current", version_choice="old")
    retry = h.plan_recovery("companion_a", "stateful", "accept_current", version_choice="target")
    await h.apply_recovery(retry.plan_id)
    assert h._version("companion_a", "stateful")["data_version"] == 2


async def test_committed_choice_remains_locked_if_its_retry_fails(tmp_path, monkeypatch):
    h, _, _, record = await interrupted(tmp_path)
    replace_record(h, None)
    plan = h.plan_recovery("companion_a", "stateful", "retry_restore")
    clear = h.journals.clear

    def crash(*args):
        raise Crash()

    monkeypatch.setattr(h.journals, "clear", crash)
    with pytest.raises(Crash):
        await h.apply_recovery(plan.plan_id)
    monkeypatch.setattr(h.journals, "clear", clear)
    retry = h.plan_recovery("companion_a", "stateful", "retry_restore")
    read = h.snapshots.read

    def broken(*args):
        raise ValueError("fictional snapshot read failure")

    monkeypatch.setattr(h.snapshots, "read", broken)
    with pytest.raises(ValueError):
        await h.apply_recovery(retry.plan_id)
    monkeypatch.setattr(h.snapshots, "read", read)
    assert h._version("companion_a", "stateful") == record["previous_version"]
    with pytest.raises(ValueError, match="same confirmed"):
        h.plan_recovery("companion_a", "stateful", "accept_current")
