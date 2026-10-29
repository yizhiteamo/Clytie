"""迁移恢复日志与用户确认解除；仅按确认计划修复版本记录，不加载插件代码。"""

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from packaging.version import InvalidVersion, Version

from clytie.sdk.immutable import freeze, thaw

from .recovery import RecoveryError, inventory
from .snapshots import SnapshotError, atomic_read, atomic_write, canonical, contained, identifier


def valid_version(value):
    if (
        not isinstance(value, dict)
        or type(value.get("data_version")) is not int
        or value["data_version"] < 1
    ):
        return False
    try:
        Version(value["plugin_version"])
        return True
    except (KeyError, TypeError, InvalidVersion):
        return False


def resolution_committed(intent, current):
    return bool(intent) and (
        intent.get("committed", False)
        or intent.get("status") == "written"
        or (
            intent.get("status") == "writing"
            and intent.get("write_required", True)
            and current == intent["version_after"]
        )
    )


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
    from .snapshots import UpgradeSnapshots

    root = Path(home).resolve()

    def version(cid, pid):
        path = contained(root, root / "companions" / cid / "plugin-versions.json")
        return json.loads(atomic_read(path)).get(pid) if path.exists() else None

    journals = Journals(UpgradeSnapshots(root, None, None), version, None)
    result = []
    for path in sorted((root / "companions").glob("*/plugin-recovery/*.json")):
        cid, pid = path.parent.parent.name, path.stem
        try:
            value = journals._for_plan(cid, pid)
            options = journals.action_options(value)
            result.append(
                {
                    "level": "error",
                    "companion_id": cid,
                    "plugin_id": pid,
                    "phase": value["phase"],
                    "step": value.get("step"),
                    "reason": value.get("error") or "recovery journal requires processing",
                    "locations": journals._positions(value),
                    "snapshot": value["snapshot"],
                    "actions": list(options),
                    "action_details": options,
                }
            )
        except (OSError, ValueError, KeyError, TypeError):
            result.append(
                {
                    "level": "error",
                    "companion_id": cid,
                    "plugin_id": pid,
                    "phase": "unknown",
                    "reason": "unreadable recovery metadata",
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
    version_before: object
    version_after: object
    version_choice: object

    def __post_init__(self):
        object.__setattr__(self, "version_before", freeze(self.version_before))
        object.__setattr__(self, "version_after", freeze(self.version_after))


class Journals:
    def __init__(self, snapshots, version_reader, version_writer):
        self.snapshots = snapshots
        self.home, self.clock, self.log = snapshots.home, snapshots.clock, snapshots.log
        self.plans = {}
        self.version_reader, self.version_writer = version_reader, version_writer

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
            if record.get("resolution"):
                intent = record["resolution"]
                if (
                    intent["operation"] not in {"retry_restore", "accept_current"}
                    or intent["status"] not in {"executing", "writing", "written", "failed"}
                    or not valid_version(intent["version_after"])
                ):
                    raise ValueError("invalid recovery choice")
            if record.get("resolution_only"):
                if (
                    record["phase"] != "restoring"
                    or record["resolution"]["operation"] != "accept_current"
                ):
                    raise ValueError("invalid resolution journal")
                if self._target(record, "state") != self.snapshots.state_path(cid, pid):
                    raise ValueError("invalid resolution state")
                return record
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
            "resolution": record.get("resolution"),
            "error": record.get("error"),
        }
        path = self.path(record["companion_id"], record["plugin_id"]).with_suffix(".audit.jsonl")
        contained(self.home, path).parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(canonical(event) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.log.info("plugin.recovery_audit", **event)

    def begin(self, plan, *, executor=None, target_version=None):
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
            "previous_version": self.version_reader(cid, pid),
            "target_version": target_version,
            "executor": executor,
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

    def restore(self, record, *, clear=True):
        cid, pid = record["companion_id"], record["plugin_id"]
        state = self._target(record, "state")
        try:
            record["phase"], record["step"] = "restoring", "decrypt"
            self.save(record)
            payload = self.snapshots.read(self.home / record["snapshot"], cid, pid)
            if state.is_dir() and self.snapshots._tree(state) == payload:
                if clear:
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
            if clear:
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
            "raw_journal": hashlib.sha256(
                self.path(record["companion_id"], record["plugin_id"]).read_bytes()
            ).hexdigest(),
            "version": self.version_reader(record["companion_id"], record["plugin_id"]),
            "directories": {
                name: inventory(path) if path.is_dir() else None for name, path in paths.items()
            },
        }
        if record.get("resolution_only"):
            return hashlib.sha256(canonical(data)).hexdigest()
        snapshot = contained(self.home, self.home / record["snapshot"])
        data["snapshot"] = (
            hashlib.sha256(snapshot.read_bytes()).hexdigest() if snapshot.is_file() else None
        )
        return hashlib.sha256(canonical(data)).hexdigest()

    def _for_plan(self, cid, pid):
        try:
            record = self.read(cid, pid)
        except SnapshotError:
            state = self.snapshots.state_path(cid, pid)
            record = {
                "companion_id": cid,
                "plugin_id": pid,
                "phase": "restoring",
                "resolution_only": True,
                "plan_id": "damaged-journal",
                "locations": {"state": {"path": state.relative_to(self.home).as_posix()}},
                "snapshot": None,
            }
        if record is None:
            raise ValueError("no recovery journal")
        return record

    def action_options(self, record):
        """status 与计划共用的可用操作判断；只读，不生成计划或创建密钥。"""
        cid, pid = record["companion_id"], record["plugin_id"]
        current = self.version_reader(cid, pid)
        intent = record.get("resolution")
        damaged = record.get("resolution_only") or not valid_version(record.get("previous_version"))
        options = {}
        if not damaged:
            try:
                self.snapshots.read(self.home / record["snapshot"], cid, pid)
            except (OSError, ValueError):
                pass
            else:
                options["retry_restore"] = {"label": "重新恢复"}
        if self._target(record, "state").is_dir():
            options["accept_current"] = {
                "label": "接受当前目录（需要输入数据版本）" if damaged else "接受当前目录",
                "requires_data_version": bool(damaged),
            }
        if resolution_committed(intent, current):
            return {key: value for key, value in options.items() if key == intent["operation"]}
        return options

    def plan(self, cid, pid, operation, *, current_code, version_choice=None):
        if operation not in {"retry_restore", "accept_current"}:
            raise ValueError("unknown recovery action")
        record = self._for_plan(cid, pid)
        state = self._target(record, "state")
        if operation not in self.action_options(record):
            raise ValueError(
                "recovery action unavailable; resume the same confirmed recovery action "
                "or check damaged journal, private state and snapshot"
            )
        current = self.version_reader(cid, pid)
        old = record.get("previous_version")
        target = record.get("target_version") or current_code
        intent = record.get("resolution")
        damaged = record.get("resolution_only") or not valid_version(old)
        committed = resolution_committed(intent, current)
        if committed:
            if intent["operation"] != operation or version_choice != intent["choice"]:
                raise ValueError("resume the same confirmed recovery action and version choice")
            after = intent["version_after"]
        elif damaged:
            if operation != "accept_current":
                raise ValueError("damaged journal permits only accept_current")
            if (
                type(version_choice) is not int
                or not 1 <= version_choice <= current_code["data_version"]
            ):
                raise ValueError("explicit data version within current code range required")
            after = {
                "plugin_version": current_code["plugin_version"],
                "data_version": version_choice,
            }
        elif current == old or current == target:
            if version_choice is not None:
                raise ValueError("normal version record must remain unchanged")
            after = current
        elif operation == "retry_restore":
            if version_choice is not None:
                raise ValueError("retry_restore uses the recorded previous version")
            after = old
        else:
            if version_choice not in {"old", "target"}:
                raise ValueError("explicit old or target version choice required")
            after = (
                old
                if version_choice == "old"
                else {
                    "plugin_version": current_code["plugin_version"],
                    "data_version": record["to_version"],
                }
            )
        try:
            if damaged:
                raise SnapshotError("unverifiable journal")
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
        if damaged:
            warning = "无法核对，该版本由用户指定"
        if current != after:
            steps.insert(-1, "write_version_record")
        fingerprint = self._fingerprint(record)
        values = (
            operation,
            cid,
            pid,
            fingerprint,
            tuple(steps),
            equal,
            warning,
            current,
            after,
            version_choice,
        )
        plan = RecoveryPlan(hashlib.sha256(canonical(values)).hexdigest(), *values)
        self.plans[plan.plan_id] = plan
        return plan

    def apply(self, plan_id):
        from .host import PlanStale

        plan = self.plans.get(plan_id)
        if plan is None:
            raise PlanStale("unknown recovery plan")
        record = self._for_plan(plan.companion_id, plan.plugin_id)
        if self._fingerprint(record) != plan.fingerprint:
            raise PlanStale("recovery journal or directories changed; regenerate and confirm")
        self.plans.pop(plan_id)
        committed = resolution_committed(record.get("resolution"), thaw(plan.version_before))
        if record.get("resolution"):
            self.audit(record, "resolution_previous_choice")
        record["plan_id"] = plan.plan_id
        record["phase"] = "restoring"
        record["resolution"] = {
            "plan_id": plan.plan_id,
            "operation": plan.operation,
            "choice": plan.version_choice,
            "version_after": thaw(plan.version_after),
            "status": "executing",
            "committed": committed,
            "write_required": plan.version_before != plan.version_after,
        }
        self.save(record)
        self.audit(record, plan.operation + "_confirmed")
        try:
            if plan.operation == "retry_restore":
                self.restore(record, clear=False)
            elif not self._target(record, "state").is_dir():
                raise PlanStale("private state missing")
            record["resolution"]["status"] = "writing"
            self.save(record)
            if self.version_reader(plan.companion_id, plan.plugin_id) != plan.version_after:
                self.version_writer(plan.companion_id, plan.plugin_id, thaw(plan.version_after))
            record["resolution"]["status"] = "written"
            record["resolution"]["committed"] = True
            self.save(record)
            self.clear(
                record,
                "restore_completed"
                if plan.operation == "retry_restore"
                else "current_directory_accepted",
            )
        except Exception as exc:
            record["error"] = type(exc).__name__
            resolution = record["resolution"]
            written = resolution_committed(
                resolution, self.version_reader(plan.companion_id, plan.plugin_id)
            )
            resolution["committed"] = written
            resolution["status"] = "written" if written else "failed"
            self.save(record)
            self.audit(record, "resolution_failed")
            raise
        return {"outcome": "migration_failed", "action": plan.operation}
