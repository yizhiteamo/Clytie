"""已安装代码的数据迁移：只读计划、确认、快照与失败恢复。"""

import asyncio
import hashlib
import inspect
import json
from dataclasses import dataclass
from types import ModuleType

from clytie.config import thaw
from clytie.sdk import CancelToken

from .context import build_context
from .dependencies import resolve
from .memory import bind_scene
from .recovery import Cleanup, failed_status
from .snapshots import atomic_write, canonical, contained, identifier
from .support import FileStorage, finish_cleanup


@dataclass(frozen=True)
class MigrationPlan:
    plan_id: str
    companion_id: str
    plugin_id: str
    from_version: int
    to_version: int
    scripts: tuple[str, ...]
    snapshot: str
    fingerprint: str


class MigrationMixin:
    def _versions_path(self, companion_id):
        return contained(
            self.home, self.home / "companions" / identifier(companion_id) / "plugin-versions.json"
        )

    def _versions(self, companion_id):
        file = self._versions_path(companion_id)
        return json.loads(file.read_bytes()) if file.exists() else {}

    def _version(self, companion_id, plugin_id):
        return self._versions(companion_id).get(plugin_id)

    def _record_version(self, companion_id, manifest):
        versions = self._versions(companion_id)
        versions[manifest.id] = {
            "plugin_version": manifest.data["version"],
            "data_version": manifest.data.get("data", {}).get("version", 1),
        }
        atomic_write(self._versions_path(companion_id), canonical(versions))

    def _load_status(self, companion_id, manifest):
        previous = self._version(companion_id, manifest.id)
        if previous is None:
            return None, None
        before, after = previous["data_version"], manifest.data.get("data", {}).get("version", 1)
        if before > after:
            return "data_newer_than_code", "update code or restore an upgrade snapshot"
        if before < after:
            missing = [
                f"migrations/to_{v}.py"
                for v in range(before + 1, after + 1)
                if not contained(
                    manifest.path.parent, manifest.path.parent / "migrations" / f"to_{v}.py"
                ).is_file()
            ]
            return ("missing_migration", missing) if missing else ("pending_migration", None)
        return None, None

    def _migration_fingerprint(self, companion_id, plugin_id):
        manifest = self.manifests[plugin_id]
        return hashlib.sha256(
            canonical(
                {
                    "code": self.snapshots._tree(manifest.path.parent),
                    "state": self.snapshots._tree(
                        self.snapshots.state_path(companion_id, plugin_id)
                    ),
                    "versions": self._versions(companion_id),
                    "graph": self._fingerprint(companion_id),
                    "layouts": thaw(self.unit_configs.get((companion_id, plugin_id))),
                }
            )
        ).hexdigest()

    def plan_migration(self, companion_id, plugin_id):
        self.refresh()
        manifest = self.manifests[plugin_id]
        runtime = self.runtimes.get((companion_id, plugin_id))
        if runtime and (
            runtime.status in {"active", "starting", "stopping", "quiescing", "migrating"}
            or any(not t.done() for t in runtime.calls)
        ):
            raise ValueError("disable the plugin and finish its calls before planning migration")
        status, detail = self._load_status(companion_id, manifest)
        if status != "pending_migration":
            raise ValueError(f"migration unavailable: {status}; {detail}")
        resolve(self.manifests, [plugin_id], active=self._active(companion_id))
        for layout in self.unit_configs.get((companion_id, plugin_id), [{"config": None}]):
            self._config(companion_id, manifest, layout["config"])
        before = self._version(companion_id, plugin_id)["data_version"]
        after = manifest.data.get("data", {}).get("version", 1)
        scripts = tuple(f"migrations/to_{v}.py" for v in range(before + 1, after + 1))
        snapshot = self.snapshots.planned_path(companion_id, plugin_id, str(before), str(after))
        fingerprint = self._migration_fingerprint(companion_id, plugin_id)
        values = (
            companion_id,
            plugin_id,
            before,
            after,
            scripts,
            snapshot.relative_to(self.home).as_posix(),
            fingerprint,
        )
        plan = MigrationPlan(hashlib.sha256(canonical(values)).hexdigest(), *values)
        if not hasattr(self, "_migration_plans"):
            self._migration_plans = {}
        self._migration_plans[plan.plan_id] = plan
        return plan

    async def apply_migration(self, plan_id, cancel=None):
        from .host import PlanStale, PluginFailure, Runtime

        cancel = cancel or CancelToken()
        async with self._lock:
            plan = getattr(self, "_migration_plans", {}).get(plan_id)
            self.refresh()
            if (
                plan is None
                or self._migration_fingerprint(plan.companion_id, plan.plugin_id)
                != plan.fingerprint
            ):
                raise PlanStale("migration plan changed; regenerate and confirm")
            cid, pid = plan.companion_id, plan.plugin_id
            old = self.runtimes.get((cid, pid))
            if old and (
                old.status in {"active", "starting", "stopping", "quiescing", "migrating"}
                or any(not t.done() for t in old.calls)
            ):
                raise PlanStale("plugin must not be running during migration")
            cancel.raise_if_cancelled()
            manifest = self.manifests[pid]
            previous = self._versions(cid)
            snapshot = self.snapshots.create(
                cid,
                pid,
                str(plan.from_version),
                str(plan.to_version),
                path=contained(self.home, self.home / plan.snapshot),
            )
            self._migration_plans.pop(plan_id)
            storage = self._storages.setdefault(
                (cid, pid), FileStorage(self.snapshots.state_path(cid, pid), self.clock)
            )
            migration = Runtime(cid, manifest, storage, manifest.priority, status="migrating")
            self.runtimes[cid, pid] = migration
            step = "context"
            try:
                layout = self.unit_configs.get((cid, pid), [{"config": None}])[0]
                migration.ctx = build_context(
                    self,
                    migration,
                    self._config(cid, manifest, layout["config"]),
                    {},
                    permissions=(),
                    migration=True,
                )
                for step in plan.scripts:
                    cancel.raise_if_cancelled()
                    script = contained(manifest.path.parent, manifest.path.parent / step)
                    module = ModuleType("_clytie_migration")
                    exec(compile(script.read_bytes(), str(script), "exec"), module.__dict__)
                    function = getattr(module, "migrate")
                    if not inspect.iscoroutinefunction(function):
                        raise TypeError("migration must be asynchronous")
                    with bind_scene():
                        await function(migration.ctx, cancel)
                    cancel.raise_if_cancelled()
                step = "start"
                self._record_version(cid, manifest)
                if not await self._start(cid, manifest, version_check=False):
                    raise PluginFailure("migrated plugin start failed")
                cancel.raise_if_cancelled()
            except (Exception, asyncio.CancelledError) as exc:
                # stop（包括启动后的取消）也处于恢复事务内；清理完成后才恢复状态。
                reason = type(exc).__name__

                async def rollback():
                    current = self.runtimes.get((cid, pid))
                    if current and current.status == "active":
                        await self._stop(cid, pid, "migration_failed")
                    atomic_write(self._versions_path(cid), canonical(previous))
                    migration.status = "migration_failed"
                    self.runtimes[cid, pid] = migration
                    self.states.setdefault(cid, {})[pid] = {
                        "status": "migration_failed",
                        "step": step,
                        "reason": reason,
                    }
                    return self.snapshots.restore(snapshot, cid, pid)

                failed, cancelled_during_cleanup = await finish_cleanup(rollback())
                self._failure(migration, step, exc)
                if cancelled_during_cleanup:
                    raise asyncio.CancelledError
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return {
                    "outcome": "migration_failed",
                    "snapshot": plan.snapshot,
                    "failed_directory": failed.relative_to(self.home).as_posix()
                    if failed
                    else None,
                    "step": step,
                    "reason": type(exc).__name__,
                }
            self.states.setdefault(cid, {}).pop(pid, None)
            base = contained(self.home, snapshot.parent)
            names = [p.name for p in base.glob("failed-*") if p.is_dir()]
            if names:
                atomic_write(contained(base, base / "resolved.json"), canonical(sorted(names)))
            return {"outcome": "migrated", "snapshot": plan.snapshot}

    def failed_state_status(self):
        return failed_status(self.home)

    def plan_cleanup(self, directories):
        if not hasattr(self, "_cleanup"):
            self._cleanup = Cleanup(self.home, self.log)
        return self._cleanup.plan(directories)

    def apply_cleanup(self, plan_id):
        if not hasattr(self, "_cleanup"):
            from .host import PlanStale

            raise PlanStale("cleanup plan unknown")
        return self._cleanup.apply(plan_id)
