"""明确分类的清理服务；管理 API 负责确认、鉴权与管理口令。"""

from pathlib import Path

from clytie.plugin_host.locking import file_lock
from clytie.plugin_host.snapshots import contained
from clytie.supervisor.common import read, write

from .old_instances import OldInstances


class CleanupService:
    def __init__(self, home, clock):
        self.home, self.clock = Path(home).resolve(), clock
        self.directory = contained(self.home, self.home / "supervisor/backup-cleanup")

    def _path(self, plan_id):
        from .journal import BackupJournal

        BackupJournal.path_for(self.home, plan_id)
        return contained(self.home, self.directory / (plan_id + ".json"))

    def fingerprint(self, plan_id):
        """Bind immutable selection while allowing durable per-file progress."""
        import hashlib

        from clytie.plugin_host.snapshots import canonical

        from .restore import digest

        plan = self.operation(plan_id)
        if plan["category"] != "regular":
            return digest(self._path(plan_id))
        selection = {
            key: plan[key]
            for key in (
                "id",
                "category",
                "created_at",
                "backup_ids",
                "repository_id",
                "eligible_ids",
            )
        }
        selection["items"] = [
            {key: item[key] for key in ("backup_id", "eligible", "record")}
            for item in plan["items"]
        ]
        return hashlib.sha256(canonical(selection)).hexdigest()

    def plan(
        self,
        category,
        *,
        old_id=None,
        preserved=None,
        operation_id=None,
        plan_id=None,
        backup_ids=None,
    ):
        if category == "regular":
            from uuid import uuid4

            from .explicit_cleanup import plan

            if old_id is not None or preserved is not None or operation_id is not None:
                raise ValueError("cleanup_category_invalid")
            plan_id = plan_id or uuid4().hex
            with file_lock(self._path(plan_id).with_suffix(".lock"), {"operation": "cleanup_plan"}):
                previous = read(self._path(plan_id), None)
                if previous is not None:
                    if previous["category"] != category or previous["backup_ids"] != backup_ids:
                        raise ValueError("cleanup_plan_conflict")
                    return previous
                return plan(self, backup_ids, plan_id)
        if (
            category != "old_instance"
            or not old_id
            or operation_id is not None
            or backup_ids is not None
        ):
            raise ValueError("cleanup_category_invalid")
        native = OldInstances(self.home, self.clock).plan_cleanup(old_id, preserved=preserved)
        result = dict(
            native, id=plan_id or native["id"], category="old_instance", native_plan_id=native["id"]
        )
        write(self._path(result["id"]), result)
        return result

    def operation(self, plan_id):
        plan = read(self._path(plan_id), None)
        if plan is None:
            raise ValueError("cleanup_plan_not_found")
        if plan["category"] == "old_instance":
            native = OldInstances(self.home, self.clock).operation(plan["native_plan_id"])
            return dict(native, id=plan["id"], category="old_instance", native_plan_id=native["id"])
        return plan

    def apply(self, plan_id, *, check=None):
        with file_lock(self._path(plan_id).with_suffix(".lock"), {"operation": "backup_cleanup"}):
            plan = self.operation(plan_id)
            if plan["category"] == "regular":
                from .explicit_cleanup import apply

                return apply(self, plan, check)
            if plan["category"] != "old_instance":
                raise ValueError("cleanup_category_invalid")
            result = OldInstances(self.home, self.clock).apply_cleanup(
                plan["native_plan_id"], check=check
            )
            return dict(result, id=plan["id"], category="old_instance", native_plan_id=result["id"])

    def recover(self, plan_id):
        with file_lock(self._path(plan_id).with_suffix(".lock"), {"operation": "cleanup_recovery"}):
            plan = self.operation(plan_id)
            if plan["category"] == "regular":
                from .explicit_cleanup import recover

                return recover(self, plan)
            if plan["category"] != "old_instance":
                raise ValueError("cleanup_category_invalid")
            native = OldInstances(self.home, self.clock).recover_cleanup(plan["native_plan_id"])
            return dict(native, id=plan["id"], category="old_instance", native_plan_id=native["id"])
