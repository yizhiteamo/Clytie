"""统一合法虚构夹具；版本适配只改变入口，不改变各版本生产算法。

legacy 对应改造前同步存储；original 和 candidate 使用各自的 StorageOwner。
调用者负责固定产品代码与导入路径。本模块不测量、不选择版本、不自动回退。
tiny 只供离线单元/组合测试，正式运行必须使用固定四规模。
"""

import asyncio
import hashlib
import json
import random
import shutil
from pathlib import Path
from types import SimpleNamespace

import yaml

import clytie
from clytie.backup.coordination import admission, companion_coordinator, run_sync
from clytie.config import load_home
from clytie.kernel.migration import LedgerRuntime
from clytie.plugin_host import PluginHost
from clytie.sdk import CancelToken
from clytie.sdk.testing import FakeContext, FakeModel, MockClock
from clytie.sdk.types import AssetRef, MemoryRecord

SCALES = {
    "small_text": (128, 8, 1024),
    "large_database": (32 * 1024, 8, 1024),
    "many_small_files": (128, 2048, 1024),
    "large_asset": (128, 1, 128 * 1024 * 1024),
}
VERSION_MODES = ("legacy", "original", "candidate")
LEDGER_TABLES = ("inbound", "record", "invocation", "intent", "attempt", "dedupe", "audit")
CONTENT_BYTES = 4096
FIXTURE_VERSION = "legal_memory_v2"
PROVIDER = """
import asyncio
from clytie.sdk import BackupTicket, BackupView
class Plugin:
    async def start(self, ctx):
        self.files = ()
        self.assets = ()
    async def stop(self):
        pass
    def backup(self):
        return self
    async def prepare(self, request, cancel):
        return BackupTicket(self.files)
    async def capture(self, ticket, epoch, cancel):
        return BackupView(epoch, 1, 'immutable_fixture_v1', ticket.handle, self.assets)
    async def materialize(self, view, sink, cancel):
        for path in view.handle:
            async def chunks(path=path):
                with path.open('rb') as stream:
                    while True:
                        block = await asyncio.to_thread(stream.read, 1024 * 1024)
                        if not block:
                            break
                        yield block
            await sink.write(path.name, chunks())
    async def release(self, ticket):
        pass
"""


def _configuration(home, product_root):
    shutil.copytree(product_root / "examples/home", home)
    plugin = home / "plugins/benchmark-snapshot"
    plugin.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "id": "benchmark-snapshot",
        "name": "虚构性能样本",
        "summary": "离线不可变文件提供者",
        "category": "example",
        "version": "1.0.0",
        "requires": {"sdk": ">=0.7,<0.11"},
        "entry": "plugin:Plugin",
        "contributes": {"backup": {"snapshot": {"factory": "backup"}}},
        "permissions": [],
        "config": {"type": "object", "additionalProperties": False},
        "backup": {"mode": "snapshot"},
    }
    (plugin / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugin / "plugin.py").write_text(PROVIDER, encoding="utf-8")
    config_path = home / "companions/companion_a/companion.yaml"
    config = yaml.safe_load(config_path.read_bytes())
    config["plugins"]["benchmark-snapshot"] = {"enabled": True, "config": {}}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    versions = {}
    for identifier in ("fake-model", "fake-channel"):
        metadata = json.loads((product_root / "plugins" / identifier / "plugin.json").read_bytes())
        versions[identifier] = {
            "plugin_version": metadata["version"],
            "data_version": metadata.get("data", {}).get("version", 1),
        }
    (home / "companions/companion_a/plugin-versions.json").write_text(
        json.dumps(versions), encoding="utf-8"
    )


async def _close(owner):
    try:
        await owner.host.close()
    finally:
        try:
            if owner.storage_owner is not None:
                await owner.storage_owner.close()
        finally:
            owner.runtime.close()


async def close(owner):
    """取消等待者也排空实际关闭；连接保持各版本原来的关闭线程归属。"""
    task = asyncio.create_task(_close(owner))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError()


async def setup(directory, scale_name, version_mode, seed, *, tiny=False, product_root=None):
    if version_mode not in VERSION_MODES:
        raise ValueError("measurement_version_mode_invalid")
    if scale_name not in SCALES or type(tiny) is not bool:
        raise ValueError("measurement_scale_invalid")
    if type(seed) is not int or seed < 0:
        raise ValueError("measurement_seed_invalid")
    imported_root = Path(clytie.__file__).resolve().parents[2]
    product_root = imported_root if product_root is None else Path(product_root).resolve()
    if product_root != imported_root:
        raise ValueError("measurement_product_root_mismatch")
    rows, files, size = SCALES[scale_name]
    if tiny:
        rows, files, size = 8, min(files, 2), 32
    root = Path(directory).absolute()
    root.mkdir(parents=True, exist_ok=False)
    home = root / "home"
    _configuration(home, product_root)
    clock = MockClock()
    report = load_home(home, repo_root=product_root)
    if not report.valid:
        raise ValueError("fictional_configuration_invalid")
    host = PluginHost(product_root, home, clock=clock)
    host.configure_report(report)
    runtime = LedgerRuntime(home, "companion_a", clock)
    owner = SimpleNamespace(
        home=home,
        cid="companion_a",
        clock=clock,
        host=host,
        runtime=runtime,
        report=report,
        management_requests=set(),
        storage_owner=None,
        store=None,
        version_mode=version_mode,
        record_count=rows,
        records=rows,
        tiny=tiny,
        model=FakeModel(FakeContext(clock=clock)),
    )
    try:
        if version_mode == "legacy":
            await runtime.open()
            owner.store = host.memory_store(owner.cid, {}, {})
        else:
            # Deliberately imported only for the selected version. Missing support
            # is an error; never silently substitute a different execution model.
            from clytie.storage.owner import StorageOwner
            from clytie.storage.runtime import CompanionResources, bind_handles

            runtime.owner_managed = True
            await runtime.open()
            owner.storage_owner = await StorageOwner.start(
                lambda: CompanionResources(host, runtime, {}),
                companion_id=owner.cid,
                clock=clock,
                coordination_path=companion_coordinator(home, owner.cid, clock).path,
            )
            await runtime.attach_owned(bind_handles(owner.storage_owner, host, runtime))
            owner.store = host.memory_resources[owner.cid][0]
        plan = host.plan_enable(owner.cid, "benchmark-snapshot")
        applied = await host.apply(plan.plan_id)
        if applied["failed"]:
            raise ValueError("fictional_provider_enable_failed")
        rng = random.Random(seed)
        store = owner.store

        def seed_database():
            with store.transaction():
                # Construction only: _save deletes previous keyword postings by
                # record_id. Its production index starts with token, otherwise
                # seeding many fresh records repeatedly scans the growing table.
                # Keep _save/transactions unchanged and remove this index before
                # returning a usable fixture. All three versions use this adapter.
                store.db.execute("CREATE INDEX measurement_seed_lookup ON keywords(record_id)")
                store.set_meta("companion_uid", "a" * 32)
                for index in range(rows):
                    store._save(
                        MemoryRecord(
                            record_id=f"{index + 1:032x}",
                            revision=1,
                            kind="fact",
                            content=rng.randbytes(CONTENT_BYTES // 2).hex(),
                            visibility="private",
                            write_mode="auto",
                            source_type="private",
                            created_at=clock.now(),
                            updated_at=clock.now(),
                            decay={"weight": 1, "frozen": False},
                            retracted=False,
                        ),
                        operator="fixture",
                    )
                store.db.execute("DROP INDEX measurement_seed_lookup")
            # Fix the construction layout before any warmup or timed request;
            # no auxiliary index or free-page cost is left in the measured input.
            store.db.execute("VACUUM")
            store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        await run_sync(store, seed_database)
        core_assets = scale_name == "large_asset"
        data = home / (
            "companions/companion_a/assets"
            if core_assets
            else "companions/companion_a/plugin-data/benchmark-snapshot"
        )
        data.mkdir(parents=True, exist_ok=True)
        paths, assets, asset_paths = [], [], []
        for index in range(files):
            path = data / f"fictional_{index:05d}.bin"
            digest = hashlib.sha256()
            with path.open("xb") as output:
                remaining = size
                while remaining:
                    block = rng.randbytes(min(1024 * 1024, remaining))
                    output.write(block)
                    digest.update(block)
                    remaining -= len(block)
            if core_assets:
                path.chmod(0o444)
                ref = AssetRef(
                    asset_id=digest.hexdigest(),
                    size=size,
                    media_type="application/octet-stream",
                    origin="fictional benchmark",
                )
                host.asset_store.register_private(owner.cid, ref, path.relative_to(home).as_posix())
                assets.append(ref)
                asset_paths.append(path)
            else:
                paths.append(path)
        provider = host.runtimes[owner.cid, "benchmark-snapshot"].plugin
        provider.files, provider.assets = tuple(paths), tuple(assets)

        def memory_counts():
            count, minimum, maximum, total = store.db.execute(
                "SELECT count(*),min(length(json_extract(data,'$.content'))),"
                "max(length(json_extract(data,'$.content'))),"
                "sum(length(json_extract(data,'$.content'))) FROM records"
            ).fetchone()
            return {
                "records": count,
                "revisions": store.db.execute("SELECT count(*) FROM revisions").fetchone()[0],
                # Fixture content is ASCII hex, so SQLite character length is
                # byte length. Do not materialize the full large database again.
                "content_bytes": total,
                "content_length_distribution": {
                    "min_bytes": minimum,
                    "max_bytes": maximum,
                    "total_bytes": total,
                    "records": count,
                },
            }

        def ledger_counts():
            ledger = runtime.ledger
            ledger.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return {
                table: ledger.db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                for table in LEDGER_TABLES
            }

        logical = await run_sync(store, memory_counts)
        ledger_rows = await run_sync(runtime.ledger, ledger_counts)
        provider_bytes = sum(path.stat().st_size for path in paths)
        asset_bytes = sum(path.stat().st_size for path in asset_paths)
        logical.update(
            content_bytes_per_record=CONTENT_BYTES,
            file_count=len(paths) + len(asset_paths),
            file_bytes=provider_bytes + asset_bytes,
            provider_files=len(paths),
            provider_bytes=provider_bytes,
            asset_registrations=len(host.asset_store.snapshot().registrations),
            asset_bytes=asset_bytes,
            ledger_rows=ledger_rows,
        )
        metrics = dict(
            logical=logical,
            ledger_rows=ledger_rows,
            memory_database_bytes=store.path.stat().st_size,
            ledger_database_bytes=runtime.path.stat().st_size,
            asset_file_bytes=asset_bytes,
            plugin_file_bytes=provider_bytes,
        )
        return owner, metrics
    except BaseException:
        await close(owner)
        raise


async def business(owner, index):
    if type(index) is not int or index < 0:
        raise ValueError("measurement_request_index_invalid")
    key = f"{index % owner.record_count + 1:032x}"
    if owner.version_mode == "legacy":
        store = owner.store
        row = store.db.execute("SELECT data FROM records WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ValueError("measurement_record_missing")
        async with admission(store):
            with store.transaction():
                store.set_meta("benchmark_counter", index)
    elif owner.version_mode in ("original", "candidate"):

        def command(resources, _):
            store = resources.memory
            row = store.db.execute("SELECT data FROM records WHERE id=?", (key,)).fetchone()
            if row is None:
                raise ValueError("measurement_record_missing")
            with store.coordination.commit(), store.transaction():
                store.set_meta("benchmark_counter", index)

        await owner.storage_owner.execute(command)
    else:
        raise ValueError("measurement_version_mode_invalid")
    return await owner.model.generate(None, CancelToken())
