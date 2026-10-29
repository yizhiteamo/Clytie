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
from .locking import MetadataLockTimeout, OperationBusy
from .memory import bind_scene
from .recovery import Cleanup, failed_status
from .snapshots import atomic_read, atomic_write, canonical, contained, identifier
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
        return self._read_versions(companion_id)

    def _read_versions(self, companion_id):
        file = self._versions_path(companion_id)
        values = json.loads(atomic_read(file)) if file.exists() else {}
        if not isinstance(values, dict):
            raise ValueError("invalid plugin version records")
        return values

    def _version(self, companion_id, plugin_id):
        return self._versions(companion_id).get(plugin_id)

    async def _record_version(self, companion_id, manifest):
        await self._write_version(
            companion_id,
            manifest.id,
            {
                "plugin_version": manifest.data["version"],
                "data_version": manifest.data.get("data", {}).get("version", 1),
            },
        )

    async def _write_version(self, companion_id, plugin_id, value):
        async with self.operations.metadata(companion_id):
            versions = self._read_versions(companion_id)
            versions[plugin_id] = value
            atomic_write(self._versions_path(companion_id), canonical(versions), retry_sharing=True)

    def _load_status(self, companion_id, manifest):
        if self.journals.path(companion_id, manifest.id).exists():
            return "recovery_incomplete", "resolve recovery journal before migration or loading"
        previous = self._version(companion_id, manifest.id)
        if previous is None:
            return None, None
        if not self.snapshots.state_path(companion_id, manifest.id).is_dir():
            return "private_state_missing", "existing version record forbids creating empty state"
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

    def _before_load(self, cid, pid):
        self.operations.require(cid, pid)
        runtime = self.runtimes.get((cid, pid))
        if runtime and runtime.status in {"migrating", "starting", "stopping", "quiescing"}:
            raise OperationBusy({"operation": runtime.status})
        try:
            record = self.journals.read(cid, pid)
            if record is not None:
                version = self._version(cid, pid)
                current = version.get("data_version") if isinstance(version, dict) else None
                if record["phase"] == "migrating" and current == record["to_version"]:
                    self.journals.clear(record, "committed_journal_removed")
                elif record["phase"] == "migrating" and current == record["from_version"]:
                    self.journals.restore(record)
                    return (
                        "migration_failed",
                        "interrupted migration restored; reload after checking code",
                    )
                else:
                    return "recovery_incomplete", "user-confirmed recovery action required"
            if self._version(cid, pid) and not self.snapshots.state_path(cid, pid).is_dir():
                return (
                    "private_state_missing",
                    "existing version record forbids creating empty state",
                )
        except (OSError, ValueError) as exc:
            if not self.journals.path(cid, pid).exists():
                raise
            return "recovery_incomplete", getattr(exc, "locations", type(exc).__name__)
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
                    "journal": self.journals.path(companion_id, plugin_id).exists(),
                    "state_exists": self.snapshots.state_path(companion_id, plugin_id).is_dir(),
                    "versions": self._versions(companion_id),
                    "graph": self._fingerprint(companion_id),
                    "layouts": thaw(self.unit_configs.get((companion_id, plugin_id))),
                }
            )
        ).hexdigest()

    def plan_migration(self, companion_id, plugin_id):
        with self.operations.hold(companion_id, [plugin_id], "plan_migration"):
            return self._plan_migration_locked(companion_id, plugin_id)

    def _plan_migration_locked(self, companion_id, plugin_id):
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
        from .host import PlanStale

        plan = getattr(self, "_migration_plans", {}).get(plan_id)
        if plan is None:
            raise PlanStale("unknown migration plan")
        try:
            with self.operations.hold(plan.companion_id, [plan.plugin_id], "migration"):
                return await self._apply_migration_locked(plan_id, cancel)
        except OperationBusy as exc:
            return exc.result()

    async def _apply_migration_locked(self, plan_id, cancel=None):
        from .host import PlanStale, Runtime

        cancel = cancel or CancelToken()
        plan = getattr(self, "_migration_plans", {}).get(plan_id)
        self.refresh()
        if (
            plan is None
            or self._migration_fingerprint(plan.companion_id, plan.plugin_id) != plan.fingerprint
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
        snapshot = self.snapshots.create(
            cid,
            pid,
            str(plan.from_version),
            str(plan.to_version),
            path=contained(self.home, self.home / plan.snapshot),
        )
        self._migration_plans.pop(plan_id)
        record = self.journals.begin(
            plan,
            executor=self.operations.executor,
            target_version={
                "plugin_version": manifest.data["version"],
                "data_version": plan.to_version,
            },
        )
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
                record["step"] = record["migration_step"] = step
                self.journals.save(record)
                script = contained(manifest.path.parent, manifest.path.parent / step)
                module = ModuleType("_clytie_migration")
                exec(compile(script.read_bytes(), str(script), "exec"), module.__dict__)
                function = getattr(module, "migrate")
                if not inspect.iscoroutinefunction(function):
                    raise TypeError("migration must be asynchronous")
                with bind_scene():
                    await function(migration.ctx, cancel)
                cancel.raise_if_cancelled()
        except (Exception, asyncio.CancelledError) as exc:
            # 仅提交前的失败恢复旧状态；恢复完成后才传播取消。
            reason = type(exc).__name__

            async def rollback():
                current = self.runtimes.get((cid, pid))
                if current and current.status == "active":
                    await self._stop(cid, pid, "migration_failed")
                migration.status = "migration_failed"
                self.runtimes[cid, pid] = migration
                self.states.setdefault(cid, {})[pid] = {
                    "status": "migration_failed",
                    "step": step,
                    "reason": reason,
                }
                record["error"] = reason
                try:
                    return self.journals.restore(record)
                except (OSError, ValueError):
                    migration.status = "recovery_incomplete"
                    self.states[cid][pid]["status"] = "recovery_incomplete"
                    raise

            failed, cancelled_during_cleanup = await finish_cleanup(rollback())
            self._failure(migration, step, exc)
            if cancelled_during_cleanup:
                raise asyncio.CancelledError
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {
                "outcome": "migration_failed",
                "snapshot": plan.snapshot,
                "failed_directory": failed.relative_to(self.home).as_posix() if failed else None,
                "step": step,
                "reason": type(exc).__name__,
            }
        # 脚本完成才提交；记录原子更新是唯一提交点。此后任何错误都不恢复旧数据。
        try:
            await self._record_version(cid, manifest)
        except asyncio.CancelledError:

            async def restore_cancelled_commit():
                migration.status = "migration_failed"
                self.states.setdefault(cid, {})[pid] = {"status": "migration_failed"}
                record["error"] = "CancelledError"
                try:
                    self.journals.restore(record)
                except (OSError, ValueError):
                    migration.status = "recovery_incomplete"
                    self.states[cid][pid]["status"] = "recovery_incomplete"
                    raise

            await finish_cleanup(restore_cancelled_commit())
            raise
        except (OSError, ValueError, MetadataLockTimeout) as exc:
            # 解锁诊断写入也可能在原子提交之后失败，按磁盘提交事实报告。
            committed = self._version(cid, pid) == record["target_version"]
            detail = (
                "version committed; reload removes residual journal"
                if committed
                else "下次加载将按 4.3 节自动恢复"
            )
            reason = f"{type(exc).__name__}: {detail}"
            migration.status = "migration_failed"
            self.states.setdefault(cid, {})[pid] = {
                "status": "migration_failed",
                "step": "write_version_record",
                "reason": reason,
            }
            self._failure(migration, "write_version_record", exc)
            return {"outcome": "migration_failed", "snapshot": plan.snapshot, "reason": reason}
        try:
            self.journals.clear(record, "migration_committed")
        except (OSError, ValueError) as exc:
            # 版本已提交：下次加载只清理残留日志，绝不回滚新数据。
            migration.status = "migration_failed"
            reason = f"{type(exc).__name__}: version committed; reload removes residual journal"
            self.states.setdefault(cid, {})[pid] = {"status": "migration_failed", "reason": reason}
            return {"outcome": "migration_failed", "snapshot": plan.snapshot, "reason": reason}
        self.states.setdefault(cid, {}).pop(pid, None)
        base = contained(self.home, snapshot.parent)
        names = [p.name for p in base.glob("failed-*") if p.is_dir()]
        if names:
            atomic_write(contained(base, base / "resolved.json"), canonical(sorted(names)))
        migration.status = "disabled"
        started = await self._start(cid, manifest, version_check=False)
        return {"outcome": "migrated" if started else "start_failed", "snapshot": plan.snapshot}

    def plan_recovery(self, companion_id, plugin_id, operation, *, version_choice=None):
        with self.operations.hold(companion_id, [plugin_id], "plan_recovery"):
            return self._plan_recovery_locked(
                companion_id, plugin_id, operation, version_choice=version_choice
            )

    def _plan_recovery_locked(self, companion_id, plugin_id, operation, *, version_choice=None):
        runtime = self.runtimes.get((companion_id, plugin_id))
        if runtime and runtime.status in {
            "active",
            "starting",
            "stopping",
            "quiescing",
            "migrating",
        }:
            raise ValueError("plugin must be stopped before recovery")
        self.refresh()
        manifest = self.manifests[plugin_id]
        return self.journals.plan(
            companion_id,
            plugin_id,
            operation,
            current_code={
                "plugin_version": manifest.data["version"],
                "data_version": manifest.data.get("data", {}).get("version", 1),
            },
            version_choice=version_choice,
        )

    async def apply_recovery(self, plan_id):
        from .host import PlanStale

        plan = self.journals.plans.get(plan_id)
        if plan is None:
            raise PlanStale("unknown recovery plan")
        try:
            with self.operations.hold(plan.companion_id, [plan.plugin_id], "recovery"):
                return await self._apply_recovery_locked(plan_id)
        except OperationBusy as exc:
            return exc.result()

    async def _apply_recovery_locked(self, plan_id):
        from .host import PlanStale

        plan = self.journals.plans.get(plan_id)
        if plan is None:
            raise PlanStale("unknown recovery plan")
        runtime = self.runtimes.get((plan.companion_id, plan.plugin_id))
        if runtime and runtime.status in {
            "active",
            "starting",
            "stopping",
            "quiescing",
            "migrating",
        }:
            raise PlanStale("plugin must be stopped before recovery")
        try:
            result = await self.journals.apply(plan_id)
        except (OSError, ValueError, asyncio.CancelledError) as exc:
            if self.journals.path(plan.companion_id, plan.plugin_id).exists():
                if runtime:
                    runtime.status = "recovery_incomplete"
                self.states.setdefault(plan.companion_id, {})[plan.plugin_id] = {
                    "status": "recovery_incomplete",
                    "reason": type(exc).__name__,
                }
            raise
        if runtime:
            runtime.status = "migration_failed"
        self.states.setdefault(plan.companion_id, {})[plan.plugin_id] = {
            "status": "migration_failed"
        }
        return result

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
