from types import SimpleNamespace

import pytest
from tests.host_helpers import enable, host, plugin

from clytie.plugin_host.registry import assemble, validate_result
from clytie.sdk import BackupTicket, BackupView


class Provider:
    async def prepare(self, request, cancel): ...
    async def capture(self, ticket, epoch, cancel): ...
    async def materialize(self, view, sink, cancel): ...
    async def release(self, ticket): ...


def runtime(provider):
    return SimpleNamespace(
        manifest=SimpleNamespace(
            id="provider",
            data={"contributes": {"backup": {"snapshot": {"factory": "backup"}}}, "config": {}},
        ),
        plugin=SimpleNamespace(backup=lambda: provider),
        priority=100,
    )


def test_backup_factory_is_registered_once_and_types_are_checked():
    provider = Provider()
    regs = assemble(runtime(provider))
    backup = [r for r in regs if r.point == "backup.snapshot"]
    assert len(backup) == 1
    assert backup[0].implementation is provider
    for method in ["prepare", "capture", "materialize", "release"]:
        with pytest.raises(TypeError):
            validate_result("backup.snapshot", method, "wrong")
    assert validate_result("backup.snapshot", "prepare", BackupTicket(object()))
    assert validate_result("backup.snapshot", "capture", BackupView("epoch", 1, "rev", object()))


@pytest.mark.parametrize("method", ["prepare", "capture", "materialize", "release"])
def test_backup_factory_rejects_sync_and_wrong_signature(method):
    bad = Provider()
    setattr(bad, method, lambda *args: None)
    with pytest.raises(TypeError):
        assemble(runtime(bad))

    async def no_arguments():
        pass

    setattr(bad, method, no_arguments)
    with pytest.raises(TypeError):
        assemble(runtime(bad))


async def test_legacy_plugin_still_enables_and_stateless_contradiction_is_reported(tmp_path):
    plugin(tmp_path, "legacy")
    plugin(tmp_path, "stateless", backup={"mode": "stateless"})
    h = host(tmp_path)
    assert h.plan_enable("companion_a", "legacy").backup_support["legacy"]["mode"] == "unsupported"
    await enable(h, "legacy")
    await enable(h, "stateless")
    assert h.is_active("companion_a", "legacy")
    assert h.backup_support("companion_a", "stateless")["reason"] is None
    data = tmp_path / "companions/companion_a/plugin-data/stateless/state.json"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text("{}")
    result = h.backup_support("companion_a", "stateless")
    assert result["reason"] == "stateless_has_data"
    assert result["supported"] is False
    assert h.is_active("companion_a", "stateless")
    await h.close()
