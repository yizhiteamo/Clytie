from pathlib import Path
from types import SimpleNamespace

import pytest

from clytie.api.management_journal import gate
from clytie.backup.runtime_capture import capture_companion
from clytie.kernel.migration import LedgerRuntime
from clytie.plugin_host import PluginHost
from clytie.sdk.testing import MockClock


@pytest.fixture
async def companion(tmp_path):
    clock = MockClock()
    runtime = LedgerRuntime(tmp_path, "companion_a", clock)
    host = PluginHost(Path(__file__).resolve().parents[1], tmp_path, clock=clock)
    await runtime.open()
    host.memory_store("companion_a", {}, {})
    owner = SimpleNamespace(
        home=tmp_path, cid="companion_a", clock=clock, runtime=runtime, host=host
    )
    try:
        yield owner
    finally:
        await host.close()
        runtime.close()


async def test_online_capture_uses_existing_owners_and_returns_group_evidence(companion):
    result = await capture_companion(companion, companion.home / "result", epoch="test")
    assert result["status"] == "group_complete"
    assert result["group"] == "memory_ledger_progress"
    assert result["epoch"] == "test"
    assert result["members"]["memory"]["schema_version"] == 2
    assert result["members"]["ledger"]["schema_version"] == 3
    assert result["requires_groups"] == ["configuration", "plugins", "assets", "management"]
    for member in result["members"].values():
        assert len(member["sha256"]) == 64
        assert (companion.home / "result" / member["path"]).is_file()


async def test_active_management_is_unavailable_without_recovery(companion):
    with gate(companion.home, companion.cid):
        result = await capture_companion(companion, companion.home / "result", epoch="test")
    assert result["status"] == "unavailable"
    assert result["reason"] == "capture_busy"
    assert not (companion.home / "result").exists()


async def test_blocked_or_missing_owner_never_opens_runtime(companion):
    blocked = companion.home / "companions/companion_a/management/blocked.json"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("{}")
    result = await capture_companion(companion, companion.home / "result", epoch="test")
    assert result["reason"] == "snapshot_unavailable"
    assert blocked.read_text() == "{}"
    companion.runtime.close()
    result = await capture_companion(companion, companion.home / "another", epoch="test")
    assert result["status"] == "unavailable"
