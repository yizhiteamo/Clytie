"""Backup operation evidence is durable independently of HTTP waiters."""

import json
import re
from pathlib import Path

from clytie.plugin_host.locking import file_lock, process_identity
from clytie.plugin_host.snapshots import atomic_read, atomic_write, canonical, contained


def write(path, value):
    # Called under the stable operation lock; retry only Windows atomic replacement.
    atomic_write(path, canonical(value), retry_sharing=True)


TERMINAL = {"committed", "cancelled", "failed", "recovery_blocked"}
PHASES = {"accepted", "preparing", "capturing", "copying", "verifying", "publishing", *TERMINAL}
RESULT_FIELDS = {
    "creation",
    "verification",
    "retention",
    "mirror",
    "restore_readiness",
    "backup_id",
    "restore_id",
    "plan_id",
    "reason",
    "missing_count",
    "bytes",
    "retention_reason",
    "mirror_reason",
}


class JournalError(ValueError):
    pass


class BackupJournal:
    def __init__(self, path, clock, value):
        self.path, self.clock, self.value = Path(path), clock, value

    @staticmethod
    def path_for(home, identifier):
        if not isinstance(identifier, str) or not re.fullmatch("[a-f0-9]{32}", identifier):
            raise JournalError("invalid_operation_id")
        home = Path(home).resolve()
        return contained(home, home / "supervisor/backup-operations" / (identifier + ".json"))

    @classmethod
    def accept(cls, home, clock, identifier, operation, arguments):
        path = cls.path_for(home, identifier)
        if operation not in {
            "create",
            "verify",
            "restore_plan",
            "restore_apply",
            "recover",
            "cleanup_plan",
            "cleanup_apply",
        }:
            raise JournalError("invalid_operation")
        with file_lock(
            path.with_suffix(".lock"), {**process_identity(), "operation": "backup_admit"}
        ):
            if path.exists():
                found = cls.load(path, clock)
                if found.value["operation"] != operation:
                    raise JournalError("operation_mismatch")
                return found, False
            value = dict(
                schema_version=1,
                id=identifier,
                operation=operation,
                phase="accepted",
                created_at=clock.now().isoformat(),
                updated_at=clock.now().isoformat(),
                executor=None,
                cancel_requested=False,
                cancel_at=None,
                arguments=arguments,
                artifacts=[],
                result={},
            )
            write(path, value)
            return cls(path, clock, value), True

    @classmethod
    def load(cls, path, clock):
        try:
            value = json.loads(atomic_read(path))
            if value["schema_version"] != 1 or value["phase"] not in PHASES:
                raise ValueError()
            if not re.fullmatch("[a-f0-9]{32}", value["id"]):
                raise ValueError()
            if Path(path).name != value["id"] + ".json":
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise JournalError("journal_invalid") from None
        return cls(path, clock, value)

    def _change(self, change):
        with file_lock(
            self.path.with_suffix(".lock"), {**process_identity(), "operation": "backup_evidence"}
        ):
            current = self.load(self.path, self.clock).value
            change(current)
            current["updated_at"] = self.clock.now().isoformat()
            write(self.path, current)
            self.value = current

    def phase(self, phase, **evidence):
        if phase not in PHASES or phase == "committed":
            raise JournalError("phase_invalid")
        if set(evidence) - {
            "executor",
            "artifacts",
            "reason",
            "envelope",
            "source_sha256",
            "delivery",
        }:
            raise JournalError("evidence_invalid")

        def update(current):
            if current["phase"] in TERMINAL:
                raise JournalError("operation_terminal")
            current.update(phase=phase, **evidence)

        self._change(update)

    def commit(self, result):
        if set(result) - RESULT_FIELDS or not all(
            value is None or isinstance(value, (str, int, bool)) for value in result.values()
        ):
            raise JournalError("result_invalid")

        def update(current):
            if current["phase"] == "committed" and current["result"] == result:
                return
            if current["phase"] in TERMINAL:
                raise JournalError("operation_terminal")
            current.update(phase="committed", result=result)

        self._change(update)

    def request_cancel(self):
        def update(current):
            if current["phase"] not in TERMINAL and not current["cancel_requested"]:
                current.update(cancel_requested=True, cancel_at=self.clock.now().isoformat())

        self._change(update)

    def public(self):
        from .errors import public_reason

        result = {
            name: self.value.get(name)
            for name in (
                "id",
                "operation",
                "phase",
                "created_at",
                "updated_at",
                "cancel_requested",
                "result",
            )
        }
        if self.value.get("reason") is not None:
            result["reason"] = public_reason(self.value["reason"])
        return result
