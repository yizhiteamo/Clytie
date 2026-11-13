"""实例重 I/O 的持久执行责任；只核验身份，不恢复业务或旧调度资格。

所有方法是同步文件控制工作，调用方须放在事件循环、Owner 与捕获门闩之外。
release 的 confirmed 和 ended 均只接受可信宿主的真实完成证明；调用前先撤销
对应执行授权。controller/session 不从请求参数取得，也不能跨重启复用。
"""

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from clytie.api.management_journal import Journal, executor_alive
from clytie.plugin_host.locking import OperationBusy, file_lock, process_identity
from clytie.plugin_host.snapshots import atomic_read, atomic_write, canonical, contained

from .io_budget import IoBudgetError
from .journal import BackupJournal

ROLES = {"operation", "recovery", "capture", "startup_recovery"}
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_OPERATION = re.compile(r"[a-f0-9]{32}\Z")
_RECHECK = "backup_io_recheck_required"


def _token(value):
    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None


def _identity(value):
    valid = (
        isinstance(value, dict)
        and set(value) == {"pid", "started_at"}
        and type(value["pid"]) is int
        and value["pid"] > 0
        and isinstance(value["started_at"], str)
        and 0 < len(value["started_at"]) <= 128
    )
    if not valid:
        return False
    try:
        return datetime.fromisoformat(value["started_at"]).utcoffset() is not None
    except (ValueError, OverflowError):
        return False


class Responsibilities:
    def __init__(self, home, clock):
        self.home, self.clock = Path(home).resolve(), clock
        self.directory = self.home / "supervisor/io-responsibility"
        self.lock_path = self.home / "supervisor/io-responsibility.lock"
        self.anchor_path = self.directory / "anchor.json"
        self.index_path = self.directory / "index.json"

    def _now(self):
        return self.clock.now().isoformat()

    @contextmanager
    def _locked(self):
        try:
            for path in (self.directory, self.lock_path, self.anchor_path, self.index_path):
                contained(self.home, path)
            existed = self.lock_path.exists()
            # An existing ledger with a missing stable lock cannot create a second
            # coordination inode while an old holder may still own the first.
            if not existed and self.directory.exists():
                raise IoBudgetError(_RECHECK)
            with file_lock(
                self.lock_path, {**process_identity(), "operation": "io_responsibility"}
            ):
                yield self._load(existed)
        except OperationBusy:
            raise IoBudgetError("backup_io_coordination_busy") from None
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            if isinstance(error, IoBudgetError):
                raise
            raise IoBudgetError(_RECHECK) from None

    def _load(self, lock_existed):
        if not self.directory.exists():
            if lock_existed:
                raise IoBudgetError(_RECHECK)
            # Directory/lock existence is an initialization marker. Interrupted
            # initialization is never mistaken for a new empty instance.
            self.directory.mkdir()
            instance = uuid4().hex
            atomic_write(self.anchor_path, canonical({"version": 1, "instance": instance}))
            value = {
                "version": 1,
                "instance": instance,
                "index": [],
                "records": {},
                "sources": {},
                "events": [],
            }
            self._save(value)
        anchor = json.loads(atomic_read(self.anchor_path))
        value = json.loads(atomic_read(self.index_path))
        if (
            not isinstance(anchor, dict)
            or set(anchor) != {"version", "instance"}
            or anchor["version"] != 1
            or not _token(anchor["instance"])
            or not isinstance(value, dict)
            or set(value)
            != {"version", "instance", "index", "records", "sources", "events", "checksum"}
            or value["version"] != 1
            or value["instance"] != anchor["instance"]
            or not isinstance(value["index"], list)
            or not isinstance(value["records"], dict)
            or not isinstance(value["sources"], dict)
            or not isinstance(value["events"], list)
            or len(value["index"]) != len(set(value["index"]))
            or set(value["index"]) != set(value["records"])
            or value["checksum"]
            != hashlib.sha256(
                canonical({key: item for key, item in value.items() if key != "checksum"})
            ).hexdigest()
        ):
            raise IoBudgetError(_RECHECK)
        events = value["events"]
        if any(
            not isinstance(event, dict)
            or set(event) != {"generation", "action", "at"}
            or event["generation"] not in value["records"]
            or event["action"] not in {"registered", "reserved", "bound", "released", "exited"}
            or not isinstance(event["at"], str)
            for event in events
        ):
            raise IoBudgetError(_RECHECK)
        creations = [
            event["generation"] for event in events if event["action"] in {"registered", "reserved"}
        ]
        if creations != value["index"]:
            raise IoBudgetError(_RECHECK)
        pairs = set()
        for generation, record in value["records"].items():
            if (
                not _token(generation)
                or not isinstance(record, dict)
                or set(record)
                != {"controller", "session", "operation", "identity", "role", "state", "legacy"}
                or not all(_token(record[key]) for key in ("controller", "session", "operation"))
                or record["role"] not in ROLES
                or record["state"] not in {"reserved", "active", "ended"}
                or type(record["legacy"]) is not bool
                or (record["identity"] is not None and not _identity(record["identity"]))
                or (record["state"] == "active" and record["identity"] is None)
            ):
                raise IoBudgetError(_RECHECK)
            pair = (record["session"], record["operation"])
            if pair in pairs:
                raise IoBudgetError(_RECHECK)
            pairs.add(pair)
            history = [event["action"] for event in events if event["generation"] == generation]
            ended = history[-1] in {"released", "exited"}
            if ended != (record["state"] == "ended"):
                raise IoBudgetError(_RECHECK)
        for relative, operation in value["sources"].items():
            path = Path(relative)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not isinstance(operation, (str, type(None)))
                or (operation is not None and not _OPERATION.fullmatch(operation))
                or not (
                    len(path.parts) == 3
                    and path.parts[:2] == ("supervisor", "backup-operations")
                    or len(path.parts) == 5
                    and path.parts[0] == "companions"
                    and path.parts[2:4] == ("management", "operations")
                )
            ):
                raise IoBudgetError(_RECHECK)
        return value

    def _save(self, value):
        value["checksum"] = hashlib.sha256(
            canonical({key: item for key, item in value.items() if key != "checksum"})
        ).hexdigest()
        atomic_write(self.index_path, canonical(value), retry_sharing=True)

    def _event(self, value, generation, action):
        value["events"].append({"generation": generation, "action": action, "at": self._now()})

    def _add(self, value, controller, session, operation, identity, role, *, legacy=False):
        generation = uuid4().hex
        value["index"].append(generation)
        value["records"][generation] = {
            "controller": controller,
            "session": session,
            "operation": operation,
            "identity": identity,
            "role": role,
            "state": "active" if identity is not None else "reserved",
            "legacy": legacy,
        }
        self._event(value, generation, "registered" if identity is not None else "reserved")
        return generation

    def _sources(self, value):
        paths = {self.home / relative for relative in value["sources"]}
        directories = [self.home / "supervisor/backup-operations"]
        companions = contained(self.home, self.home / "companions")
        if companions.exists():
            for companion in companions.iterdir():
                contained(self.home, companion)
                if companion.is_dir():
                    directories.append(companion / "management/operations")
        for directory in directories:
            contained(self.home, directory)
            if directory.exists():
                paths.update(path for path in directory.iterdir() if path.suffix == ".json")
        return sorted(paths)

    @staticmethod
    def _block(operation, reason, *, role=None):
        return {
            "operation": operation,
            "role": role,
            "reason": reason,
            "check": "verify_executor_identity"
            if reason in {"executor_active", "executor_unverified"}
            else "inspect_responsibility_evidence",
        }

    def _recheck(self, value, controller):
        before = canonical(value)
        blockers = []
        for path in self._sources(value):
            relative = path.relative_to(self.home).as_posix()
            operation = path.stem if _OPERATION.fullmatch(path.stem) else None
            value["sources"][relative] = operation
            try:
                if operation is None:
                    raise ValueError()
                contained(self.home, path)
                kind = BackupJournal if relative.startswith("supervisor/") else Journal
                journal = kind.load(path, self.clock)
            except (OSError, ValueError, TypeError, KeyError):
                blockers.append(self._block(operation, "journal_unverified"))
                continue
            # Only an original responsibility (including a pre-dispatch reserve)
            # covers this journal. A verifier never hides its original executor.
            original_known = any(
                record["operation"] == operation and record["role"] == "operation"
                for record in value["records"].values()
            )
            original_identity = journal.value.get("executor")
            original_identity = original_identity if _identity(original_identity) else None
            if any(
                record["legacy"]
                and record["operation"] == operation
                and record["role"] == "operation"
                and record["identity"] != original_identity
                for record in value["records"].values()
            ):
                # Old workers could overwrite executor during verification. That
                # is not evidence that the separately recorded original ended,
                # nor proof of which new execution now owns native resources.
                blockers.append(self._block(operation, "journal_executor_changed"))
            if not original_known:
                self._add(
                    value,
                    "legacy",
                    hashlib.sha256(relative.encode()).hexdigest(),
                    operation,
                    original_identity,
                    "operation",
                    legacy=True,
                )
            if "recovery_executor" in journal.value:
                identity = journal.value["recovery_executor"]
                identity = identity if _identity(identity) else None
                recovery_known = any(
                    record["operation"] == operation
                    and record["role"] in {"recovery", "startup_recovery"}
                    and record["identity"] == identity
                    for record in value["records"].values()
                )
                if not recovery_known:
                    self._add(
                        value,
                        "legacy",
                        hashlib.sha256(canonical([relative, "recovery", identity])).hexdigest(),
                        operation,
                        identity,
                        "recovery",
                        legacy=True,
                    )
        executions = []
        for generation, record in value["records"].items():
            identity_state = None
            if record["state"] != "ended" and record["identity"] is not None:
                try:
                    identity_state = executor_alive(record["identity"])
                except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                    identity_state = None
                if identity_state is False:
                    record["state"] = "ended"
                    self._event(value, generation, "exited")
            if record["state"] != "ended" and (
                record["controller"] != controller
                or record["state"] == "active"
                and identity_state is None
            ):
                blockers.append(
                    self._block(
                        record["operation"],
                        "executor_active" if identity_state is True else "executor_unverified",
                        role=record["role"],
                    )
                )
            executions.append(
                {
                    "operation": record["operation"],
                    "role": record["role"],
                    "state": record["state"],
                    "data_recovery": "not_checked",
                }
            )
        if canonical(value) != before:
            self._save(value)
        return {
            "state": "blocked" if blockers else "ready",
            "blockers": blockers,
            "executions": executions,
            "observed_at": self._now(),
        }

    def check(self, controller):
        try:
            if not _token(controller) or controller == "legacy":
                raise IoBudgetError(_RECHECK)
            with self._locked() as value:
                return self._recheck(value, controller)
        except IoBudgetError as error:
            reason = (
                "coordination_busy"
                if error.code == "backup_io_coordination_busy"
                else "responsibility_evidence_unverified"
            )
            return {
                "state": "blocked",
                "blockers": [self._block(None, reason)],
                "executions": [],
                "observed_at": self._now(),
            }

    @staticmethod
    def _find(value, controller, session, operation):
        for generation, record in value["records"].items():
            if (record["controller"], record["session"], record["operation"]) == (
                controller,
                session,
                operation,
            ):
                return generation, record
        raise IoBudgetError("backup_io_execution_unknown")

    def _register(self, controller, session, operation, identity, role):
        if (
            not all(_token(token) for token in (controller, session, operation))
            or controller == "legacy"
            or role not in ROLES
            or (identity is not None and not _identity(identity))
        ):
            raise IoBudgetError("backup_io_request_invalid")
        with self._locked() as value:
            state = self._recheck(value, controller)
            if state["state"] != "ready":
                raise IoBudgetError("backup_io_legacy_active")
            if any(
                record["session"] == session and record["operation"] == operation
                for record in value["records"].values()
            ):
                raise IoBudgetError("backup_io_execution_reused")
            generation = self._add(value, controller, session, operation, identity, role)
            self._save(value)
            return generation

    def register(self, controller, session, operation, identity, role):
        if not _identity(identity):
            raise IoBudgetError("backup_io_request_invalid")
        return self._register(controller, session, operation, identity, role)

    def reserve(self, controller, session, operation, role):
        return self._register(controller, session, operation, None, role)

    def bind(self, controller, session, operation, identity):
        if not _identity(identity):
            raise IoBudgetError("backup_io_request_invalid")
        with self._locked() as value:
            if self._recheck(value, controller)["state"] != "ready":
                raise IoBudgetError("backup_io_legacy_active")
            generation, record = self._find(value, controller, session, operation)
            if record["state"] != "reserved":
                raise IoBudgetError("backup_io_execution_reused")
            record.update(identity=identity, state="active")
            self._event(value, generation, "bound")
            self._save(value)
            return generation

    def release(self, controller, session, operation, *, confirmed=False):
        if confirmed is not True:
            raise IoBudgetError("backup_io_exit_unconfirmed")
        with self._locked() as value:
            self._recheck(value, controller)
            generation, record = self._find(value, controller, session, operation)
            if record["state"] != "ended":
                record["state"] = "ended"
                self._event(value, generation, "released")
                self._save(value)

    def ended(self, controller, session):
        """调用者已真实 wait 对应执行者；不是 EOF 或控制器自己的退出。"""
        with self._locked() as value:
            self._recheck(value, controller)
            selected = [
                (generation, record)
                for generation, record in value["records"].items()
                if record["controller"] == controller and record["session"] == session
            ]
            if not selected:
                raise IoBudgetError("backup_io_execution_unknown")
            for generation, record in selected:
                if record["state"] != "ended":
                    record["state"] = "ended"
                    self._event(value, generation, "exited")
            self._save(value)
