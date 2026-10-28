"""迁移恢复日志与用户确认解除；不修改版本记录，不加载插件代码。"""

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .recovery import RecoveryError, inventory
from .snapshots import SnapshotError, atomic_write, canonical, contained, identifier


def journal_path(home, companion, plugin):
    return contained(
        home,
        Path(home)
        / "companions"
        / identifier(companion)
        / "plugin-recovery"
        / (identifier(plugin) + ".json"),
    )


def blocks_cleanup(home, directory):
    relative = directory.relative_to(home)
    cid, pid = relative.parts[2:4]
    path = journal_path(home, cid, pid)
    if not path.exists():
        return False
    try:
        value = json.loads(path.read_bytes())
        failed = value["locations"]["failed"]["path"]
        return contained(home, Path(home) / failed) == directory
    except (OSError, ValueError, KeyError, TypeError):
        raise SnapshotError("recovery journal unreadable; cleanup refused") from None


def recovery_status(home):
    root = Path(home).resolve()
    result = []
    for path in sorted((root / "companions").glob("*/plugin-recovery/*.json")):
        cid, pid = path.parent.parent.name, path.stem
        try:
            value = json.loads(contained(root, path).read_bytes())
            positions = {}
            for name, item in value["locations"].items():
                target = contained(root, root / item["path"])
                positions[name] = {"path": item["path"], "exists": target.exists()}
            result.append(
                {
                    "level": "error",
                    "companion_id": cid,
                    "plugin_id": pid,
                    "phase": value["phase"],
                    "step": value.get("step"),
                    "reason": value.get("error") or "recovery journal requires processing",
                    "locations": positions,
                    "snapshot": value["snapshot"],
                    "actions": ["retry_restore", "accept_current"],
                }
            )
        except (OSError, ValueError, KeyError, TypeError):
            result.append(
                {
                    "level": "error",
                    "companion_id": cid,
                    "plugin_id": pid,
                    "phase": "unknown",
                    "reason": "unreadable recovery journal",
                    "journal": path.relative_to(root).as_posix(),
                    "actions": [],
                }
            )
    return result


@dataclass(frozen=True)
class RecoveryPlan:
    plan_id: str
    operation: str
    companion_id: str
    plugin_id: str
    fingerprint: str
    steps: tuple[str, ...]
    matches_snapshot: bool | None
    warning: str | None


class Journals:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.home, self.clock, self.log = snapshots.home, snapshots.clock, snapshots.log
        self.plans = {}

    def path(self, cid, pid):
        return journal_path(self.home, cid, pid)

    def read(self, cid, pid):
        path = self.path(cid, pid)
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_bytes())
            if record["companion_id"] != cid or record["plugin_id"] != pid:
                raise ValueError("journal identity mismatch")
            if record["phase"] not in {"migrating", "restoring"}:
                raise ValueError("unknown recovery phase")
            if not (
                isinstance(record["from_version"], int)
                and isinstance(record["to_version"], int)
                and record["from_version"] < record["to_version"]
            ):
                raise ValueError("invalid journal version range")
            base = self.home / "backups" / "plugin-upgrades" / cid / pid
            snapshot = contained(base, self.home / record["snapshot"])
            if snapshot.parent != base or snapshot.suffix != ".enc":
                raise ValueError("invalid snapshot location")
            state = self.snapshots.state_path(cid, pid)
            for name, item in record["locations"].items():
                target = contained(self.home, self.home / item["path"])
                if name == "state" and target != state:
                    raise ValueError("invalid private state location")
                if name == "failed" and (
                    target.parent != base or not target.name.startswith("failed-")
                ):
                    raise ValueError("invalid failed directory location")
                if name == "temporary" and (
                    target.parent != state.parent or not target.name.startswith(".restore-")
                ):
                    raise ValueError("invalid temporary directory location")
            if set(record["locations"]) != {"state", "failed", "temporary"}:
                raise ValueError("incomplete recovery locations")
            return record
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SnapshotError("invalid recovery journal; loading is blocked") from exc

    def _target(self, record, name):
        return contained(self.home, self.home / record["locations"][name]["path"])

    def _positions(self, record):
        return {
            name: {"path": item["path"], "exists": self._target(record, name).exists()}
            for name, item in record["locations"].items()
        }

    def save(self, record):
        record["locations"] = self._positions(record)
        record["at"] = self.clock.now().isoformat()
        atomic_write(self.path(record["companion_id"], record["plugin_id"]), canonical(record))

    def audit(self, record, action):
        event = {
            "action": action,
            "plan_id": record["plan_id"],
            "companion_id": record["companion_id"],
            "plugin_id": record["plugin_id"],
            "at": self.clock.now().isoformat(),
        }
        path = self.path(record["companion_id"], record["plugin_id"]).with_suffix(".audit.jsonl")
        contained(self.home, path).parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(canonical(event) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.log.info("plugin.recovery_audit", **event)

    def begin(self, plan):
        cid, pid = plan.companion_id, plan.plugin_id
        if self.path(cid, pid).exists():
            raise SnapshotError("existing recovery journal blocks migration")
        state = self.snapshots.state_path(cid, pid)
        snapshot = contained(self.home, self.home / plan.snapshot)
        paths = {
            "state": state,
            "failed": snapshot.parent / ("failed-" + snapshot.stem),
            "temporary": state.parent / (".restore-" + pid + "-" + uuid4().hex),
        }
        record = {
            "plan_id": plan.plan_id,
            "companion_id": cid,
            "plugin_id": pid,
            "from_version": plan.from_version,
            "to_version": plan.to_version,
            "snapshot": plan.snapshot,
            "phase": "migrating",
            "step": "snapshot_verified",
            "error": None,
            "locations": {
                name: {"path": path.relative_to(self.home).as_posix()}
                for name, path in paths.items()
            },
        }
        self.save(record)
        return record

    def clear(self, record, action):
        # 审计先于删除：崩溃后至少保留已授权/已提交的动作记录。
        self.audit(record, action)
        self.path(record["companion_id"], record["plugin_id"]).unlink()

    def restore(self, record):
        cid, pid = record["companion_id"], record["plugin_id"]
        state = self._target(record, "state")
        try:
            record["phase"], record["step"] = "restoring", "decrypt"
            self.save(record)
            payload = self.snapshots.read(self.home / record["snapshot"], cid, pid)
            if state.is_dir() and self.snapshots._tree(state) == payload:
                self.clear(record, "restore_completed")
                return None
            temporary = self._target(record, "temporary")
            valid = temporary.is_dir() and self.snapshots._tree(temporary) == payload
            if not valid:
                if temporary.exists():
                    record.setdefault("retained_temporaries", []).append(
                        record["locations"]["temporary"]["path"]
                    )
                    temporary = state.parent / (".restore-" + pid + "-" + uuid4().hex)
                    record["locations"]["temporary"]["path"] = temporary.relative_to(
                        self.home
                    ).as_posix()
                    self.save(record)
                temporary.mkdir(parents=True, exist_ok=False)
                for name in payload["directories"]:
                    contained(temporary, temporary / name).mkdir(parents=True, exist_ok=True)
                for name, entry in payload["files"].items():
                    atomic_write(
                        contained(temporary, temporary / name), base64.b64decode(entry["data"])
                    )
                if self.snapshots._tree(temporary) != payload:
                    raise SnapshotError("restored private state verification failed")
            record["step"] = "temporary_verified"
            self.save(record)
            failed = self._target(record, "failed")
            if state.exists():
                stem, suffix = failed.name, 0
                while failed.exists():
                    suffix += 1
                    failed = failed.with_name(stem + f"-{suffix}")
                record["locations"]["failed"]["path"] = failed.relative_to(self.home).as_posix()
                self.save(record)
                contained(self.home, state).rename(contained(self.home, failed))
            record["step"] = "failed_retained"
            self.save(record)
            contained(self.home, temporary).rename(contained(self.home, state))
            record["step"] = "state_restored"
            self.save(record)
            self.clear(record, "restore_completed")
            return failed if failed.exists() else None
        except Exception as exc:
            record["error"] = type(exc).__name__
            try:
                self.save(record)
            except Exception:
                # 原日志仍保留；绝不因二次记录失败继续改动目录。
                pass
            raise RecoveryError(self._positions(record)) from exc

    def _fingerprint(self, record):
        paths = {name: self._target(record, name) for name in record["locations"]}
        data = {
            "record": record,
            "directories": {
                name: inventory(path) if path.is_dir() else None for name, path in paths.items()
            },
        }
        snapshot = contained(self.home, self.home / record["snapshot"])
        data["snapshot"] = (
            hashlib.sha256(snapshot.read_bytes()).hexdigest() if snapshot.is_file() else None
        )
        return hashlib.sha256(canonical(data)).hexdigest()

    def plan(self, cid, pid, operation):
        if operation not in {"retry_restore", "accept_current"}:
            raise ValueError("unknown recovery action")
        record = self.read(cid, pid)
        if record is None:
            raise ValueError("no recovery journal")
        state = self._target(record, "state")
        if operation == "accept_current" and not state.is_dir():
            raise ValueError("private state missing; cannot accept current directory")
        try:
            payload = self.snapshots.read(self.home / record["snapshot"], cid, pid)
            equal = state.is_dir() and self.snapshots._tree(state) == payload
        except (OSError, ValueError):
            if operation == "retry_restore":
                raise
            equal = None
        steps = ["remove_journal"]
        if operation == "retry_restore":
            steps = ["verify_snapshot"]
            if not equal:
                temporary = self._target(record, "temporary")
                reusable = temporary.is_dir() and self.snapshots._tree(temporary) == payload
                steps.append("reuse_verified_temporary" if reusable else "decrypt_temporary")
                if state.exists():
                    steps.append("retain_failed_state")
                steps.append("activate_snapshot")
            steps.append("remove_journal")
        warning = (
            None
            if equal
            else "该插件将使用与快照不同的数据"
            if equal is False
            else "无法验证快照一致性；该插件将使用当前目录数据"
        )
        fingerprint = self._fingerprint(record)
        values = (operation, cid, pid, fingerprint, tuple(steps), equal, warning)
        plan = RecoveryPlan(hashlib.sha256(canonical(values)).hexdigest(), *values)
        self.plans[plan.plan_id] = plan
        return plan

    def apply(self, plan_id):
        from .host import PlanStale

        plan = self.plans.get(plan_id)
        if plan is None:
            raise PlanStale("unknown recovery plan")
        record = self.read(plan.companion_id, plan.plugin_id)
        if record is None or self._fingerprint(record) != plan.fingerprint:
            raise PlanStale("recovery journal or directories changed; regenerate and confirm")
        self.plans.pop(plan_id)
        self.audit(record, plan.operation + "_confirmed")
        if plan.operation == "retry_restore":
            self.restore(record)
        else:
            if not self._target(record, "state").is_dir():
                raise PlanStale("private state missing")
            self.clear(record, "current_directory_accepted")
        return {"outcome": "migration_failed", "action": plan.operation}
