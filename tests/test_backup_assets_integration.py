import hashlib
import json
from types import SimpleNamespace

import pytest
import yaml

from clytie.assets import AssetStore
from clytie.backup.container import pack
from clytie.backup.repository import SourceRepository
from clytie.backup.restore import RestoreManager
from clytie.backup.runtime_capture import capture_online
from clytie.backup.source import capture_instance
from clytie.config import load_home
from clytie.kernel.migration import LedgerRuntime
from clytie.kernel.service import Kernel
from clytie.plugin_host import PluginHost
from clytie.sdk import CancelToken
from clytie.sdk.types import ToolCall

from .host_helpers import enable, plugin
from .test_backup_source import ROOT, fictional
from .test_kernel_service import new_record

SOURCE = """
from clytie.sdk import BackupTicket, BackupView
from clytie.sdk.types import Plan, ToolResult
class Tool:
    def __init__(self, owner):
        self.owner = owner
    def idempotency_key(self, call, invocation):
        return call.call_id
    async def prepare(self, call, invocation):
        return Plan(summary='fictional asset', heavy=False, data={'call_id': call.call_id, 'text': call.arguments['text']})
    async def execute(self, plan, invocation, cancel):
        ref = await self.owner.ctx.asset_writer.put(plan.data['text'].encode(), 'text/plain', 'tool')
        self.owner.extra = await self.owner.ctx.asset_writer.put(b'fictional snapshot product', 'text/plain', 'tool')
        if self.owner.ctx.config.get('artifact'):
            await self.owner.ctx.asset_writer.put(b'fictional configured product', 'text/plain', 'tool')
        self.owner.last = ref
        return ToolResult(call_id=plan.data['call_id'], status='ok', content='done', data={}, assets=[ref])
class Plugin:
    async def start(self, ctx):
        self.ctx, self.last, self.extra = ctx, None, None
    async def stop(self):
        pass
    def create_tool(self):
        return Tool(self)
    def backup(self):
        return self
    async def prepare(self, request, cancel):
        return BackupTicket(object())
    async def capture(self, ticket, epoch, cancel):
        return BackupView(epoch, 1, 'r', ticket.handle, (self.last, self.extra) if self.last else ())
    async def materialize(self, view, sink, cancel):
        pass
    async def release(self, ticket):
        pass
"""


def restore(captured, stage, tmp_path, clock):
    packages = tmp_path / "packages"
    packages.mkdir()
    package, key = packages / "fictional.clytie-backup", tmp_path / "fictional.key"
    key.write_bytes(b"k" * 32)
    pack(stage, package, key.read_bytes(), captured["manifest"])
    repository = SourceRepository.initialize(packages, clock, owner_instance_id="fictional")
    repository.register(
        dict(backup_id="fictional", path=str(package), identity=repository.identity(package))
    )
    manager = RestoreManager(tmp_path / "maintenance", clock, reserve_bytes=0)
    manager.previews.bind_source(
        package,
        dict(root=str(packages), repository_id=repository.repository_id, backup_id="fictional"),
        confirmed=True,
    )
    work, target = tmp_path / "work", tmp_path / "restored"
    work.mkdir()
    plan = manager.plan(package, key, target, work)
    assert manager.apply(plan["id"])["data_state"] == "ready_for_switch"
    return target, plan


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("config_asset", [False, True])
@pytest.mark.parametrize("concurrent", [False, True])
async def test_production_tool_ledger_online_encrypted_restore_authorization(
    tmp_path, shared, config_asset, concurrent, late=False
):
    home, clock = fictional(tmp_path)
    declaration = json.loads((ROOT / "plugins/echo-tool/plugin.json").read_bytes())["contributes"][
        "tools"
    ][0]
    declaration.update(factory="create_tool", name="artifact")
    credential_marker = "x-clytie-secret"
    annotation = {"type": "object", "properties": {}, "additionalProperties": False}
    effective = {}
    if config_asset:
        annotation["properties"] = {
            "artifact": {"type": "string", "x-clytie-asset": True},
            "credential": dict(type="string", **{credential_marker: True}),
        }
        annotation["required"] = ["artifact", "credential"]
        effective = {
            "artifact": hashlib.sha256(b"fictional configured product").hexdigest(),
            "credential": "harmless",
        }
    plugin(
        home,
        "asset-tool",
        SOURCE,
        permissions=["assets.write"],
        backup={"mode": "snapshot"},
        config=annotation,
        contributes={"tools": [declaration], "backup": {"snapshot": {"factory": "backup"}}},
    )
    path = home / "companions/companion_a/companion.yaml"
    config = yaml.safe_load(path.read_bytes())
    config["plugins"]["asset-tool"] = {"enabled": True, "config": effective}
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    report = load_home(home, repo_root=ROOT)
    assert report.valid, report.errors
    host = PluginHost(ROOT, home, clock=clock)
    host.configure_report(report)
    runtime = LedgerRuntime(home, "companion_a", clock)
    await runtime.open()
    host.memory_store("companion_a", {}, {})
    await enable(host, "asset-tool")
    kernel = Kernel(
        runtime.ledger,
        clock,
        {},
        permissions=lambda *_: True,
        quota=lambda *_: True,
        config={"task_reactions": {"enabled": False}},
    )
    try:
        record, invocation, _ = await new_record(kernel)
        capability = host.registrations("companion_a", "tool")[0].implementation
        result = await kernel.execute(
            record["trace_id"],
            "asset-tool",
            ToolCall(call_id="call", tool="artifact", arguments={"text": "fictional product"}),
            capability,
            invocation,
        )
        assert result.status == "ok"
        ref = result.assets[0]
        snapshot_ref = capability.owner.extra
        captured_ids = {ref.asset_id, snapshot_ref.asset_id}
        if config_asset:
            captured_ids.add(effective["artifact"])
        if late:
            late_plan = await capability.prepare(
                ToolCall(call_id="call", tool="artifact", arguments={"text": "late product"}),
                invocation,
            )
            late_result = await capability.execute(late_plan, invocation, CancelToken())
            invocation_id = runtime.ledger.rows("invocation")[0]["invocation_id"]
            assert not runtime.ledger.finish_invocation(invocation_id, late_result)
            captured_ids.add(late_result.assets[0].asset_id)
            # Only the production late-result audit holds this dependency;
            # neither the completed invocation nor the provider declares it.
            capability.owner.last = ref
            assert runtime.ledger.rows("invocation")[0]["asset_ids"] == [ref.asset_id]
            assert not runtime.ledger.rows("intent")
        if shared:
            host.asset_store.share("companion_a", ref.asset_id, ("companion_b",))
        assert (
            runtime.ledger.rows("invocation")[0]["result"]["assets"][0]["asset_id"] == ref.asset_id
        )
        owner = SimpleNamespace(
            home=home,
            cid="companion_a",
            clock=clock,
            host=host,
            runtime=runtime,
            report=report,
            management_requests=set(),
        )
        later_assets = []
        if concurrent:
            provider = host.registrations("companion_a", "backup.snapshot")[0].implementation
            original = provider.materialize

            async def while_materializing(view, sink, cancel):
                # The real capture gate and SQLite views are already fixed. A new
                # business invocation must commit without changing this backup.
                later_record, later_invocation, _ = await new_record(kernel, name="later")
                later = await kernel.execute(
                    later_record["trace_id"],
                    "asset-tool",
                    ToolCall(call_id="later", tool="artifact", arguments={"text": "later asset"}),
                    capability,
                    later_invocation,
                )
                assert later.status == "ok"
                later_assets.extend(later.assets)
                await original(view, sink, cancel)

            provider.materialize = while_materializing
        stage = tmp_path / "stage"
        captured = await capture_instance(
            home,
            clock,
            stage,
            source_instance_id="fictional",
            epoch="epoch_a",
            online_ids={"companion_a"},
            online=lambda cid, work, epoch: capture_online(owner, work, epoch),
        )
        assert captured["manifest"]["completeness"] == "complete", captured["missing"]
        assert {asset["asset_id"] for asset in captured["manifest"]["assets"]} == captured_ids
        assert bool(captured["manifest"]["companions"][0]["configuration_assets"]) == config_asset
        target, _ = restore(captured, stage, tmp_path, clock)
        if config_asset:
            restored_config = yaml.safe_load(
                (target / "companions/companion_a/companion.yaml").read_bytes()
            )
            assert "credential" not in restored_config["plugins"]["asset-tool"]["config"]
        restored = AssetStore(target, clock)
        assert await restored.bind("companion_a").read(ref.asset_id) == b"fictional product"
        assert await restored.bind("companion_a").info(ref.asset_id) == ref
        assert (
            await restored.bind("companion_a").read(snapshot_ref.asset_id)
            == b"fictional snapshot product"
        )
        if config_asset:
            assert (
                await restored.bind("companion_a").read(effective["artifact"])
                == b"fictional configured product"
            )
        assert {
            (row.companion_id, row.asset_id): row for row in restored.snapshot().registrations
        } == {
            (row.companion_id, row.asset_id): row
            for row in host.asset_store.snapshot().registrations
            if row.asset_id in captured_ids
        }
        if late:
            assert (
                await restored.bind("companion_a").read(late_result.assets[0].asset_id)
                == b"late product"
            )
        if concurrent:
            assert len(runtime.ledger.rows("invocation")) == 2
            assert len(later_assets) == 1
            with pytest.raises(KeyError):
                await restored.bind("companion_a").info(later_assets[0].asset_id)
        if shared:
            assert await restored.bind("companion_b").read(ref.asset_id) == b"fictional product"
        else:
            with pytest.raises(KeyError):
                await restored.bind("companion_b").read(ref.asset_id)
        import sqlite3

        with sqlite3.connect(target / "companions/companion_a/ledger/ledger.sqlite3") as db:
            saved = json.loads(db.execute("SELECT data FROM invocation").fetchone()[0])
            assert db.execute("SELECT count(*) FROM invocation").fetchone()[0] == 1
        assert saved["result"]["assets"][0]["asset_id"] == ref.asset_id
    finally:
        await kernel.reactions.drain()
        await kernel.bus.drain()
        await host.close()
        runtime.close()


async def test_late_tool_asset_in_audit_survives_online_backup_and_restore(tmp_path):
    await test_production_tool_ledger_online_encrypted_restore_authorization(
        tmp_path, shared=False, config_asset=False, concurrent=False, late=True
    )
