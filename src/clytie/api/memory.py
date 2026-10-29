"""记忆管理 API；CLI 只负责参数和交互，不持有导入业务规则。"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from clytie.clock import SystemClock
from clytie.config import load_home, resolve_home, thaw
from clytie.memory.package import Exports
from clytie.memory.store import MemoryStore
from clytie.memory.transfer import Transfers
from clytie.plugin_host.snapshots import contained, identifier


class MemoryAPI:
    def __init__(self, home=None, *, repo_root=None, clock=None, importers=None):
        self.home = Path(resolve_home(home)).resolve()
        self.repo_root, self.clock = repo_root, clock or SystemClock()
        self.importers = importers or {}

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
        with self.open(companion_id, readonly=True) as store:
            return await Transfers(store, self.home, companion_id, importers=self.importers).plan(
                source, **options
            )

    async def apply(self, plan_id, **options):
        if len(plan_id) != 32 or any(c not in "0123456789abcdef" for c in plan_id):
            raise ValueError("invalid plan id")
        path = contained(self.home, self.home / "run" / "memory-plans" / f"{plan_id}.json")
        plan = json.loads(path.read_bytes())
        with self.open(plan["companion_id"]) as store:
            return await Transfers(
                store, self.home, plan["companion_id"], importers=self.importers
            ).apply(plan_id, **options)

    def plan_kind(self, plan_id):
        if len(plan_id) != 32 or any(c not in "0123456789abcdef" for c in plan_id):
            raise ValueError("invalid plan id")
        path = contained(self.home, self.home / "run" / "memory-plans" / f"{plan_id}.json")
        return json.loads(path.read_bytes())["kind"]

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
