"""记忆管理 API；CLI 只负责参数和交互，不持有导入业务规则。"""

import json
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

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
    def __init__(self, home=None, *, repo_root=None, clock=None, importers=None):
        self.home = Path(resolve_home(home)).resolve()
        self.repo_root, self.clock = repo_root, clock or SystemClock()
        self.importers = importers or {}

    @asynccontextmanager
    async def importer_scope(self, companion_id, selected):
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
        report = load_home(self.home, repo_root=self.repo_root)
        if not report.valid or companion_id not in report.plugin_configs:
            raise ValueError("实例配置无效或伴侣不存在")
        if isinstance(self.clock, SystemClock):
            self.clock.local_tz = ZoneInfo(report.instance["timezone"])
        path = contained(
            self.home, self.home / "companions" / companion_id / "memory" / "memory.sqlite3"
        )
        store = MemoryStore(
            path,
            clock=self.clock,
            config=thaw(report.plugin_configs[companion_id]["config"]),
            instance=thaw(report.instance),
            readonly=readonly,
            transient=not path.exists(),
        )
        try:
            yield store
        finally:
            store.close()

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
                return await Transfers(store, self.home, companion_id, importers=importers).plan(
                    source, **options
                )

    async def apply(self, plan_id, **options):
        if len(plan_id) != 32 or any(c not in "0123456789abcdef" for c in plan_id):
            raise ValueError("invalid plan id")
        path = contained(self.home, self.home / "run" / "memory-plans" / f"{plan_id}.json")
        plan = json.loads(path.read_bytes())
        async with self.importer_scope(plan["companion_id"], plan.get("importer")) as importers:
            with self.open(plan["companion_id"]) as store:
                return await Transfers(
                    store, self.home, plan["companion_id"], importers=importers
                ).apply(plan_id, **options)

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
