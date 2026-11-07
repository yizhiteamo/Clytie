import asyncio
from types import SimpleNamespace

import pytest
import yaml

from clytie.backup.coordination import transaction
from clytie.backup.restore import validate_contents
from clytie.backup.runtime_capture import capture_online
from clytie.backup.source import capture_instance
from clytie.clock import SystemClock
from clytie.config import load_home
from clytie.kernel.migration import LedgerRuntime
from clytie.plugin_host import PluginHost

from .host_helpers import enable, plugin
from .test_backup_source import ROOT, fictional

SOURCE = """
import asyncio
from clytie.sdk import BackupTicket, BackupView
class Plugin:
    async def start(self, ctx):
        self.ctx = ctx
        self.entered = asyncio.Event()
        self.phases = []
    async def stop(self):
        pass
    def backup(self):
        return self
    async def prepare(self, request, cancel):
        self.phases.append('prepare')
        return BackupTicket('fictional-state')
    async def capture(self, ticket, epoch, cancel):
        self.phases.append('capture')
        self.entered.set()
        await self.ctx.clock.sleep(0.05)
        return BackupView(epoch, 1, 'revision_a', ticket.handle)
    async def materialize(self, view, sink, cancel):
        self.phases.append('materialize')
        async def chunks():
            yield b'fictional-state'
        await sink.write('state.bin', chunks())
    async def release(self, ticket):
        self.phases.append('release')
"""


@pytest.fixture
async def online(tmp_path):
    home, _ = fictional(tmp_path)
    plugin(
        home,
        "snapshot-test",
        source=SOURCE,
        backup={"mode": "snapshot"},
        contributes={"backup": {"snapshot": {"factory": "backup"}}},
    )
    config = home / "companions/companion_a/companion.yaml"
    document = yaml.safe_load(config.read_bytes())
    document["plugins"]["snapshot-test"] = {"enabled": True, "config": {}}
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    clock = SystemClock()
    report = load_home(home, repo_root=ROOT)
    assert report.valid, report.errors
    host = PluginHost(ROOT, home, clock=clock)
    host.configure_report(report)
    runtime = LedgerRuntime(home, "companion_a", clock)
    await runtime.open()
    host.memory_store("companion_a", {}, {})
    holder = await enable(host, "snapshot-test")
    owner = SimpleNamespace(
        home=home,
        cid="companion_a",
        clock=clock,
        host=host,
        runtime=runtime,
        report=report,
        management_requests=set(),
    )
    try:
        yield owner, holder
    finally:
        await host.close()
        runtime.close()


async def test_online_full_provider_uses_live_object_and_fixed_db_view(online, tmp_path):
    owner, holder = online
    store = owner.host.memory_resources[owner.cid][0]
    order = []
    original_prepare = holder.plugin.prepare
    original_materialize = holder.plugin.materialize

    async def prepare(*args):
        with store.transaction():
            store.set_meta("prepared_outside_gate", True)
        return await original_prepare(*args)

    async def materialize(*args):
        with store.transaction():
            store.set_meta("materialized_outside_gate", True)
        return await original_materialize(*args)

    holder.plugin.prepare = prepare
    holder.plugin.materialize = materialize

    async def callback(cid, work, epoch):
        assert cid == owner.cid
        return await capture_online(owner, work, epoch)

    async def write():
        await holder.plugin.entered.wait()
        async with transaction(store):
            store.set_meta("fictional_write", True)
        order.append("written")

    async def response():
        await holder.plugin.entered.wait()
        await owner.clock.sleep(0.01)
        order.append("response")

    result, _, _ = await asyncio.gather(
        capture_instance(
            owner.home,
            owner.clock,
            tmp_path / "stage",
            source_instance_id="fictional",
            online_ids={owner.cid},
            online=callback,
            epoch="epoch_a",
        ),
        write(),
        response(),
    )
    assert result["manifest"]["completeness"] == "complete", result["missing"]
    assert order == ["response", "written"]
    assert holder.plugin.phases == ["prepare", "capture", "materialize", "release"]
    assert not owner.management_requests
    assert not owner.backup_provider_sessions
    assert (
        tmp_path / "stage/companions/companion_a/plugin-data/snapshot-test/state.bin"
    ).read_bytes() == b"fictional-state"
    validate_contents(tmp_path / "stage", result, repo_root=ROOT)
    import sqlite3

    with sqlite3.connect(tmp_path / "stage/companions/companion_a/memory/memory.sqlite3") as db:
        assert db.execute("SELECT value FROM metadata WHERE key='prepared_outside_gate'").fetchone()
        assert not db.execute("SELECT value FROM metadata WHERE key='fictional_write'").fetchone()
        assert not db.execute(
            "SELECT value FROM metadata WHERE key='materialized_outside_gate'"
        ).fetchone()


async def test_online_provider_failure_is_partial_without_stopping_plugin(online, tmp_path):
    owner, holder = online

    async def broken(ticket, epoch, cancel):
        raise ValueError("fictional failure")

    holder.plugin.capture = broken
    work = tmp_path / "work"
    work.mkdir()
    result = await capture_online(owner, work, "epoch_a")
    assert any(item["component"].endswith(":snapshot-test") for item in result["missing"])
    assert holder.status == "active"
    assert holder.plugin.phases == ["prepare", "release"]
    assert (work / "companions/companion_a/memory/memory.sqlite3").is_file()


async def test_online_capture_budget_failure_does_not_fall_back(online, tmp_path):
    owner, holder = online

    async def slow(ticket, epoch, cancel):
        await owner.clock.sleep(0.4)

    holder.plugin.capture = slow

    async def callback(cid, work, epoch):
        return await capture_online(owner, work, epoch)

    result = await capture_instance(
        owner.home,
        owner.clock,
        tmp_path / "stage",
        source_instance_id="fictional",
        online_ids={owner.cid},
        online=callback,
        epoch="epoch_a",
    )
    assert result["manifest"]["completeness"] == "partial"
    assert result["missing"] == [{"component": "companion:companion_a", "reason": "capture_busy"}]
    assert not (tmp_path / "stage/companions/companion_a/memory/memory.sqlite3").exists()
    assert holder.status == "active"
    assert holder.plugin.phases == ["prepare", "release"]


async def test_online_memory_scope_excludes_providers_and_progress(online, tmp_path):
    import sqlite3

    owner, holder = online

    async def callback(cid, work, epoch):
        return await capture_online(owner, work, epoch, scope="memory")

    result = await capture_instance(
        owner.home,
        owner.clock,
        tmp_path / "stage",
        source_instance_id="fictional",
        scope="memory",
        cid=owner.cid,
        online_ids={owner.cid},
        online=callback,
        epoch="epoch_a",
    )
    assert result["manifest"]["completeness"] == "complete", result["missing"]
    assert len(result["files"]) == 1
    assert holder.plugin.phases == []
    with sqlite3.connect(tmp_path / "stage/companions/companion_a/memory/memory.sqlite3") as db:
        assert not db.execute("SELECT 1 FROM recollect_progress").fetchone()
