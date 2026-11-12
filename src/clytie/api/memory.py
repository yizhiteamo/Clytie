"""记忆管理 API；CLI 只负责参数和交互，不持有导入业务规则。"""

import hashlib
import json
import sqlite3
from contextlib import asynccontextmanager, contextmanager, nullcontext
from pathlib import Path
from zoneinfo import ZoneInfo

from clytie.api.management_io import management_command as run_sync
from clytie.api.management_journal import owns_runtime
from clytie.clock import SystemClock
from clytie.config import load_home, resolve_home, thaw
from clytie.memory.package import Exports
from clytie.memory.store import MemoryStore
from clytie.memory.transfer import Transfers
from clytie.plugin_host.snapshots import contained, identifier
from clytie.plugin_host.support import timed


class HostedImporter:
    def __init__(self, host, companion_id, registration):
        self.host, self.cid, self.reg = host, companion_id, registration

    async def detect(self, source):
        return await timed(
            self.host.invoke(
                self.cid, self.reg.plugin_id, self.reg.point, self.reg.key, source, method="detect"
            ),
            self.host.clock,
            30,
        )

    async def read(self, source, cancel):
        rows = await self.host.invoke(
            self.cid,
            self.reg.plugin_id,
            self.reg.point,
            self.reg.key,
            source,
            cancel,
            method="read",
        )
        for row in rows:
            cancel.raise_if_cancelled()
            yield row


class MemoryAPI:
    def __init__(self, home=None, *, repo_root=None, clock=None, importers=None, hosted=None):
        self.home = Path(resolve_home(home)).resolve()
        self.repo_root, self.clock = repo_root, clock or SystemClock()
        self.importers = importers or {}
        self.hosted = hosted

    async def command(self, companion_id, function, *args, **kwargs):
        """在线调用复用所有者；独立管理进程保留当前线程同步实现。"""
        if self.hosted is None:
            return function(companion_id, *args, **kwargs)
        with self.open(companion_id) as store:
            return await run_sync(store, function, companion_id, *args, **kwargs)

    async def file_command(self, companion_id, function, *args, **kwargs):
        from clytie.api.management_io import file_call

        if self.hosted is None:
            return await file_call(None, function, *args, **kwargs)
        with self.open(companion_id) as store:
            return await file_call(store, function, *args, **kwargs)

    @asynccontextmanager
    async def importer_scope(self, companion_id, selected):
        if self.hosted is not None and selected is not None:
            host = self.hosted.host
            reg = next(
                (
                    r
                    for r in host.registrations(companion_id, "memory.importer")
                    if r.plugin_id == selected
                ),
                None,
            )
            if reg is None:
                raise ValueError("importer unavailable")
            yield {selected: HostedImporter(host, companion_id, reg)}
            return
        if selected is None or selected in self.importers:
            yield self.importers
            return
        from clytie.plugin_host import PluginHost
        from clytie.plugin_host.host import PluginFailure

        report = load_home(self.home, repo_root=self.repo_root)
        if not report.valid or companion_id not in report.plugin_configs:
            raise ValueError("实例配置无效或伴侣不存在")
        config = thaw(report.plugin_configs[companion_id]["config"])
        plugins = config.get("plugins", {})
        if not plugins.get(selected, {}).get("enabled", False):
            raise ValueError("导入器未启用")
        host = PluginHost(
            self.repo_root or Path(__file__).resolve().parents[3], self.home, clock=self.clock
        )
        # 独立管理调用仅启动所选导入器及其必需依赖，不启动伴侣、平台或模型配置。
        host.configure(
            companion_id, plugins=plugins, instance_plugins=thaw(report.instance.get("plugins", {}))
        )
        try:
            plan = host.plan_enable(companion_id, selected)
            if not plan.ok or any(
                not plugins.get(pid, {}).get("enabled", False) for pid in plan.steps
            ):
                raise ValueError("导入器或必需依赖不可用，请先检查启用配置")
            result = await host.apply(plan.plan_id)
            if result.get("failed") or not host.is_active(companion_id, selected):
                raise ValueError("导入器启动失败")
            reg = next(
                (
                    r
                    for r in host.registrations(companion_id, "memory.importer")
                    if r.plugin_id == selected
                ),
                None,
            )
            if reg is None:
                raise ValueError("所选插件未声明导入器")
            yield {selected: HostedImporter(host, companion_id, reg)}
        except PluginFailure as exc:
            raise ValueError("导入器调用失败，未写入记忆") from exc
        finally:
            await host.close()

    @contextmanager
    def open(self, companion_id, *, readonly=False):
        identifier(companion_id)
        if self.hosted is not None:
            if companion_id != self.hosted.cid:
                raise ValueError("wrong companion")
            yield self.hosted.host.memory_store(companion_id)
            return
        report = load_home(self.home, repo_root=self.repo_root)
        if not report.valid or companion_id not in report.plugin_configs:
            raise ValueError("实例配置无效或伴侣不存在")
        if isinstance(self.clock, SystemClock):
            self.clock.local_tz = ZoneInfo(report.instance["timezone"])
        path = contained(
            self.home, self.home / "companions" / companion_id / "memory" / "memory.sqlite3"
        )
        from clytie.backup.coordination import companion_coordinator

        store = MemoryStore(
            path,
            clock=self.clock,
            config=thaw(report.plugin_configs[companion_id]["config"]),
            instance=thaw(report.instance),
            readonly=readonly,
            transient=not path.exists(),
            coordination=companion_coordinator(self.home, companion_id, self.clock),
        )
        try:
            yield store
        finally:
            store.close()

    def thaw_record(self, companion_id, record_id):
        from clytie.memory.fade import reinforce
        from clytie.memory.migration import migrate
        from clytie.memory.transfer import runtime_lock

        with (
            nullcontext()
            if self.hosted or owns_runtime()
            else runtime_lock(self.home, companion_id, "memory_thaw")
        ):
            with self.open(companion_id) as store:
                record = store.get_raw(record_id)
                if record is None or record.retracted:
                    raise ValueError("记忆不存在")
                migrate(store, self.home, companion_id, None)
                from clytie.api.management_journal import (
                    begin_database,
                    finish_commit,
                    transaction_committed,
                )

                result = {"record_id": record_id, "thawed": True}
                begin_database(store)
                with store.transaction():
                    reinforce(store, [record], transaction=False)
                    store.audit("memory.thawed", record_id=record_id)
                    transaction_committed(store, result)
                finish_commit(result)
                return result

    async def export_online(self, companion_id, out, **options):
        from clytie.storage.handle import StoreHandle

        with self.open(companion_id) as store:
            exports = Exports(store, self.home, companion_id)
            if isinstance(store, StoreHandle):
                return await exports.export_owned(out, **options)
            return exports.export(out, **options)

    def export(self, companion_id, out, *, passphrase=None, format="encrypted"):
        with self.open(companion_id) as store:
            return Exports(store, self.home, companion_id).export(
                out, passphrase=passphrase, format=format
            )

    def new_recovery_code(self, companion_id, *, confirmed=False):
        if companion_id is None:
            report = load_home(self.home, repo_root=self.repo_root)
            if not report.valid or not report.companions:
                raise ValueError("实例配置无效或没有伴侣")
            companion_id = report.companions[0]["id"]
        with self.open(companion_id) as store:
            return Exports(store, self.home, companion_id).rotate_recovery(confirmed=confirmed)

    async def plan(self, companion_id, source, **options):
        async with self.importer_scope(companion_id, options.get("importer")) as importers:
            with self.open(companion_id, readonly=True) as store:
                return await Transfers(
                    store,
                    self.home,
                    companion_id,
                    importers=importers,
                    runtime_owned=self.hosted is not None or owns_runtime(),
                ).plan(source, **options)

    async def backup_plan(
        self,
        companion_id,
        backup_id,
        *,
        source_repository=None,
        key_path=None,
        _source_repository_confirmed=False,
        **options,
    ):
        from clytie.backup.memory_source import BackupMemorySource

        source, binding = await self.file_command(
            companion_id,
            self._backup_source,
            backup_id,
            source_repository,
            key_path,
            _source_repository_confirmed,
        )
        if set(options) - {"mode", "choices", "confirm_identity"}:
            raise ValueError("invalid_request")
        adapter = BackupMemorySource(self.home, self.clock, companion_id, binding)
        with self.open(companion_id, readonly=True) as store:
            return await Transfers(store, self.home, companion_id, source_adapter=adapter).plan(
                source, **options
            )

    def _backup_source(self, backup_id, source_repository, key_path, _source_repository_confirmed):
        from clytie.backup.storage import BackupStorage

        if source_repository is not None:
            from clytie.backup.repository import SourceRepository

            if source_repository.get("backup_id") != backup_id or not key_path:
                raise ValueError("backup_memory_source_invalid")
            verified = SourceRepository(source_repository["root"], self.clock).binding(
                source_repository,
                confirmed=_source_repository_confirmed,
            )
            source = verified["source"]
            key_file = Path(key_path).resolve()
        else:
            storage = BackupStorage(self.home, self.clock)
            row = storage.get(backup_id)
            if (
                row["scope"] != "memory"
                or row["completeness"] != "complete"
                or not storage.valid(row)
            ):
                raise ValueError("backup_memory_source_invalid")
            source = row["path"]
            key_file = contained(self.home, self.home / "secrets/backup.key")
        with key_file.open("rb") as stream:
            material = stream.read(33)
        if len(material) != 32:
            raise ValueError("backup_key_invalid")
        binding = {"backup_id": backup_id, "key_fingerprint": hashlib.sha256(material).hexdigest()}
        if source_repository is not None:
            binding.update(source_repository=source_repository, key_path=str(key_file))
        return source, binding

    @staticmethod
    def _backup_validate(store, transfers, plan, home):
        from clytie.memory.transfer import database_fingerprint, source_fingerprint

        if (
            source_fingerprint(plan["source"]) != plan["source_hash"]
            or database_fingerprint(store) != plan["database"]
            or transfers.binding_fingerprint() != plan["configuration"]
        ):
            raise ValueError("plan_stale")
        binding = plan["backup_binding"]
        key_file = (
            Path(binding["key_path"])
            if "key_path" in binding
            else contained(home, home / "secrets/backup.key")
        )
        with key_file.open("rb") as stream:
            material = stream.read(33)
        if (
            len(material) != 32
            or hashlib.sha256(material).hexdigest() != plan["backup_binding"]["key_fingerprint"]
        ):
            raise ValueError("plan_stale")

    async def backup_content(self, plan):
        from clytie.backup.memory_source import BackupMemorySource
        from clytie.backup.previews import PreviewCatalog

        cid = plan["companion_id"]
        with self.open(cid, readonly=True) as store:
            transfers = Transfers(
                store,
                self.home,
                cid,
                source_adapter=BackupMemorySource(
                    self.home, self.clock, cid, plan["backup_binding"]
                ),
            )
            reader = PreviewCatalog(self.home, self.clock).reading(
                "memory",
                plan["id"],
                validate=lambda: self._backup_validate(store, transfers, plan, self.home),
            )
            await run_sync(store, reader.__enter__)
            try:
                loaded = await transfers._load(plan["source"])

                def content():
                    self._backup_validate(store, transfers, plan, self.home)
                    return {
                        "id": plan["id"],
                        "content": {
                            key: {
                                "package": record.content,
                                "current": store.get_raw(key).content
                                if store.get_raw(key)
                                else None,
                            }
                            for key, record in loaded["records"].items()
                        },
                    }

                return await run_sync(store, content)
            finally:
                await run_sync(store, reader.__exit__, None, None, None)

    async def apply(self, plan_id, **options):
        if len(plan_id) != 32 or any(c not in "0123456789abcdef" for c in plan_id):
            raise ValueError("invalid plan id")
        path = contained(self.home, self.home / "run" / "memory-plans" / f"{plan_id}.json")
        plan = await self.file_command(
            self.hosted.cid if self.hosted else None, lambda: json.loads(path.read_bytes())
        )
        async with self.importer_scope(plan["companion_id"], plan.get("importer")) as importers:
            with self.open(plan["companion_id"]) as store:
                adapter = None
                if plan.get("source_type") == "backup":
                    from clytie.api.management_journal import current
                    from clytie.backup.memory_source import BackupMemorySource
                    from clytie.backup.previews import PreviewCatalog, PreviewError

                    journal = current.get()
                    if journal is None:
                        raise ValueError("backup_apply_requires_tracked_operation")
                    adapter = BackupMemorySource(
                        self.home, self.clock, plan["companion_id"], plan["backup_binding"]
                    )
                transfers = Transfers(
                    store,
                    self.home,
                    plan["companion_id"],
                    importers=importers,
                    runtime_owned=self.hosted is not None or owns_runtime(),
                    source_adapter=adapter,
                )
                if adapter is not None:
                    catalog = PreviewCatalog(self.home, self.clock)

                    def accept():
                        accepted, _ = catalog.accept(
                            "memory",
                            plan_id,
                            journal.value["id"],
                            validate=lambda: self._backup_validate(
                                store, transfers, plan, self.home
                            ),
                        )
                        if accepted != journal.value["id"]:
                            raise PreviewError("plan_in_use", operation_id=accepted)
                        journal.value["backup_execution"] = {
                            "plan_id": plan_id,
                            "operation_id": accepted,
                        }
                        journal.save()
                        return accepted

                    accepted = await run_sync(store, accept)
                result = await transfers.apply(plan_id, **options)
                if adapter is not None:
                    await self.file_command(plan["companion_id"], catalog.release, accepted)
                return result

    def plan_kind(self, plan_id):
        if len(plan_id) != 32 or any(c not in "0123456789abcdef" for c in plan_id):
            raise ValueError("invalid plan id")
        path = contained(self.home, self.home / "run" / "memory-plans" / f"{plan_id}.json")
        plan = json.loads(path.read_bytes())
        return "importer" if plan.get("importer") else plan["kind"]

    def undo(self, companion_id, import_id, **options):
        if companion_id is None:
            report = load_home(self.home, repo_root=self.repo_root)
            matches = []
            for companion in report.companions:
                path = contained(
                    self.home,
                    self.home / "companions" / companion["id"] / "memory" / "memory.sqlite3",
                )
                if not path.is_file():
                    continue
                db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    if db.execute("SELECT 1 FROM imports WHERE id=?", (import_id,)).fetchone():
                        matches.append(companion["id"])
                finally:
                    db.close()
            if len(matches) != 1:
                raise ValueError("导入编号不存在或对应多个伴侣，请明确 --companion")
            companion_id = matches[0]
        with self.open(companion_id, readonly=True) as store:
            return Transfers(store, self.home, companion_id).undo(import_id, **options)
