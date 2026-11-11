"""Durable management phases; request bodies and secret results are never journaled."""

import asyncio
import base64
import hashlib
import json
import sqlite3
import threading
from contextvars import ContextVar
from uuid import uuid4

from clytie.plugin_host.locking import process_identity
from clytie.plugin_host.snapshots import contained, identifier
from clytie.supervisor.common import read, write

current = ContextVar("memory_management", default=None)


class ManagementCancelled(Exception):
    """Cooperative cancellation observed before a commit point."""


TERMINAL = {"committed", "cancelled", "interrupted", "failed", "recovery_blocked"}
SAFE_RESULT_FIELDS = {"import_id", "plan_id", "thawed"}


def safe_result(result):
    if not isinstance(result, dict):
        return {}
    return {
        key: value
        for key, value in result.items()
        if key in SAFE_RESULT_FIELDS
        and (isinstance(value, bool) or isinstance(value, str) and len(value) <= 64)
    }


class Journal:
    def __init__(self, path, clock, value):
        self.path, self.clock, self.value = path, clock, value
        self.cancelled = threading.Event()
        from clytie.sdk import CancelToken

        self.cancel_token = CancelToken()
        self.runtime_owned = False
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None

    @classmethod
    def create(cls, home, cid, operation, clock, operation_id=None):
        identifier(cid)
        operation_id = operation_id or uuid4().hex
        if len(operation_id) != 32 or any(c not in "0123456789abcdef" for c in operation_id):
            raise ValueError("invalid operation id")
        path = contained(
            home, home / "companions" / cid / "management/operations" / (operation_id + ".json")
        )
        if path.exists():
            raise ValueError("operation already exists")
        value = {
            "version": 1,
            "id": operation_id,
            "companion_id": cid,
            "operation": operation,
            "state": "accepted",
            "committed": False,
            "delivery": "unconfirmed",
            "executor": process_identity(),
            "created_at": clock.now().isoformat(),
            "result": {},
        }
        journal = cls(path, clock, value)
        journal.save()
        return journal

    @classmethod
    def load(cls, path, clock):
        value = read(path)
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or value.get("id") != path.stem
            or value.get("companion_id") != path.parent.parent.parent.name
            or not isinstance(value.get("operation"), str)
            or value.get("state")
            not in TERMINAL
            | {"accepted", "preparing", "materializing", "transaction", "publishing"}
            or type(value.get("committed")) is not bool
            or value.get("delivery") != "unconfirmed"
            or not isinstance(value.get("result"), dict)
            or value.get("result") != safe_result(value.get("result"))
        ):
            raise ValueError("invalid management journal")
        return cls(path, clock, value)

    @property
    def block_path(self):
        return self.path.parent.parent / "blocked.json"

    def save(self):
        self.value["updated_at"] = self.clock.now().isoformat()
        write(self.path, self.value)

    def phase(self, state):
        if self.value["committed"]:
            return
        self.value["state"] = state
        self.save()

    def cancel(self):
        self.cancelled.set()
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.cancel_token.cancel, "management_cancelled")
        else:
            self.cancel_token.cancel("management_cancelled")

    @property
    def cancel_path(self):
        return self.path.parent.parent / "cancel" / (self.value["id"] + ".json")

    def request_cancel(self):
        if not self.cancel_path.exists():
            write(
                self.cancel_path,
                {"operation_id": self.value["id"], "at": self.clock.now().isoformat()},
            )

    def checkpoint(self):
        if self.cancel_path.exists():
            self.cancel()
        if self.cancelled.is_set() and not self.value["committed"]:
            raise ManagementCancelled()

    def database(self, path):
        self.value["database"] = str(path.resolve())
        self.value["database_existed"] = path.exists()
        self.save()

    def mark_transaction(self, store, result):
        if not store.db.in_transaction:
            raise RuntimeError("management evidence requires the business transaction")
        store.audit(
            "management_committed", operation_id=self.value["id"], result=safe_result(result)
        )

    def snapshot(self, path, data):
        """Persist verified snapshot identity before publishing it or changing SQLite."""
        self.value["snapshot"] = {
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        self.save()

    def snapshot_verified(self):
        from pathlib import Path

        snapshot = self.value.get("snapshot")
        if snapshot is None:
            return self.value["operation"] != "import/apply" or not self.value["committed"]
        try:
            path = Path(snapshot["path"])
            root = self.path.parents[4] / "backups/memory" / self.value["companion_id"]
            contained(root, path)
            return (
                path.is_file()
                and not path.is_symlink()
                and path.resolve() == path
                and hashlib.sha256(path.read_bytes()).hexdigest() == snapshot["sha256"]
            )
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def prepare_file(self, path, data, result=None):
        self.checkpoint()
        self.value["file"] = {
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(data).hexdigest(),
            "result": safe_result(result),
            "previous_sha256": hashlib.sha256(path.read_bytes()).hexdigest()
            if path.exists()
            else None,
        }
        self.phase("publishing")

    def prepare_public(self, path, public):
        self.value["public_change"] = {
            "path": str(path.resolve()),
            "new": base64.b64encode(public).decode("ascii"),
            "previous": base64.b64encode(path.read_bytes()).decode("ascii")
            if path.exists()
            else None,
        }
        self.save()

    def prepare_file_audit(self, store, kind, **fields):
        store.materialize()
        self.value["file_audit"] = {
            "database": str(store.path.resolve()),
            "kind": kind,
            "fields": fields,
        }
        self.save()

    def _database(self, path, store, mode):
        if store is not None:
            if store.path.resolve() != path.resolve():
                raise ValueError("management_database_mismatch")
            return store.db
        return sqlite3.connect(path.resolve().as_uri() + "?mode=" + mode, uri=True)

    def finish_file_audit(self, store=None):
        """Complete only a recorded file-publication audit, never replay the operation."""
        from pathlib import Path

        evidence = self.value.get("file_audit")
        if evidence is None:
            return
        if evidence["kind"] not in {"memory_export", "memory_recovery_rotated"}:
            raise ValueError("invalid file audit kind")
        path = Path(evidence["database"])
        expected = self.path.parents[2] / "memory/memory.sqlite3"
        if path != expected.resolve() or path.is_symlink() or path.resolve() != path:
            raise ValueError("invalid file audit database")
        db = self._database(path, store, "rw")
        try:
            db.execute("BEGIN IMMEDIATE")
            if [tuple(row) for row in db.execute("PRAGMA quick_check")] != [("ok",)]:
                raise ValueError("invalid file audit database")
            entries = [json.loads(row[0]) for row in db.execute("SELECT data FROM audit")]
            if not any(
                row.get("operation_id") == self.value["id"] and row.get("kind") == evidence["kind"]
                for row in entries
            ):
                entry = dict(
                    kind=evidence["kind"],
                    at=self.clock.now().isoformat(),
                    operation_id=self.value["id"],
                    **evidence["fields"],
                )
                db.execute("INSERT INTO audit(data) VALUES(?)", (json.dumps(entry),))
                generation = db.execute(
                    "SELECT value FROM metadata WHERE key='generation'"
                ).fetchone()
                db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES('generation',?)",
                    (json.dumps((json.loads(generation[0]) if generation else 0) + 1),),
                )
            if store is not None:
                from clytie.storage.owner import before_storage_commit

                before_storage_commit()
            db.commit()
        finally:
            if store is None:
                db.close()
            elif db.in_transaction:
                db.rollback()

    def file_audit_exists(self, store=None):
        from pathlib import Path

        evidence = self.value.get("file_audit")
        if evidence is None:
            return False
        path = Path(evidence["database"])
        expected = self.path.parents[2] / "memory/memory.sqlite3"
        if path != expected.resolve() or path.is_symlink() or path.resolve() != path:
            raise ValueError("invalid file audit database")
        db = self._database(path, store, "ro")
        try:
            return any(
                entry.get("operation_id") == self.value["id"]
                and entry.get("kind") == evidence["kind"]
                and all(entry.get(key) == value for key, value in evidence["fields"].items())
                for entry in (json.loads(row[0]) for row in db.execute("SELECT data FROM audit"))
            )
        finally:
            if store is None:
                db.close()

    def recover_file(self, artifact, store=None):
        from clytie.plugin_host.locking import file_lock

        home = self.path.parents[4]
        with file_lock(home / "run/memory-recovery.lock", process_identity()):
            return self._recover_file(artifact, store)

    def _recover_file(self, artifact, store=None):
        from pathlib import Path

        path = Path(artifact["path"])
        if path.is_symlink() or path.resolve() != path:
            return self.blocked("artifact_unsafe")
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        published = actual == artifact["sha256"]
        if published:
            self.value["committed"] = True
            self.save()
        try:
            audited = self.file_audit_exists(store)
        except (OSError, ValueError, sqlite3.Error, KeyError):
            return self.blocked("file_audit_unverified")
        if audited and not published:
            self.value["committed"] = True
            self.save()
        if not audited and not self._recover_public(published):
            return self.blocked("public_key_unverified")
        if published:
            try:
                self.finish_file_audit(store)
                if self.value.get("backup_preview") is not None:
                    from clytie.backup.memory_source import publish_preview

                    plan = json.loads(path.read_bytes())
                    if (
                        plan["id"] != self.value["backup_preview"]["plan_id"]
                        or plan["companion_id"] != self.value["companion_id"]
                    ):
                        raise ValueError("backup_preview_mismatch")
                    publish_preview(self.path.parents[4], self.clock, plan)
            except (OSError, ValueError, sqlite3.Error, KeyError):
                return self.blocked("file_audit_unverified")
            self.committed(artifact.get("result"))
            return self.public()
        if actual == artifact.get("previous_sha256"):
            self.phase("interrupted")
            return self.public()
        return self.blocked("artifact_unverified")

    def temporary(self, path):
        path = path.absolute()
        if path.suffix != ".pending" or path.exists() or path.resolve() != path:
            raise ValueError("unsafe management temporary path")
        self.value.setdefault("temporary_files", []).append(str(path))
        self.save()

    def cleanup_temporaries(self):
        from pathlib import Path

        for name in self.value.get("temporary_files", []):
            path = Path(name)
            if path.suffix != ".pending" or path.is_symlink() or path.resolve() != path:
                return False
            if path.exists():
                if not path.is_file():
                    return False
                path.unlink()
        return True

    def recover_public(self, published):
        from clytie.plugin_host.locking import file_lock

        if self.value.get("public_change") is None:
            return True
        home = self.path.parents[4]
        path = contained(home, home / "run/memory-recovery.lock")
        with file_lock(path, {**process_identity(), "operation": "memory_export_recovery"}):
            return self._recover_public(published)

    def _recover_public(self, published):
        from pathlib import Path

        from clytie.plugin_host.snapshots import atomic_write

        change = self.value.get("public_change")
        if change is None:
            return True
        path = Path(change["path"])
        if path.is_symlink() or path.resolve() != path:
            return False
        new = base64.b64decode(change["new"], validate=True)
        old = (
            base64.b64decode(change["previous"], validate=True)
            if change["previous"] is not None
            else None
        )
        actual = path.read_bytes() if path.exists() else None
        if published:
            return actual == new
        if actual == old:
            return True
        if actual != new:
            return False
        if old is None:
            path.unlink()
        else:
            atomic_write(path, old)
        return True

    def committed(self, result=None):
        saved = safe_result(self.value.get("result")) | safe_result(result)
        self.value.update(state="committed", committed=True, result=saved)
        self.save()

    def blocked(self, reason):
        self.value.update(state="recovery_blocked", reason=reason)
        self.save()
        write(
            self.block_path,
            {
                "operation_id": self.value["id"],
                "code": "management_recovery_blocked",
                "reason": reason,
            },
        )
        return self.public()

    def public(self):
        return {
            key: self.value[key]
            for key in (
                "id",
                "companion_id",
                "operation",
                "state",
                "committed",
                "delivery",
                "result",
            )
        } | {"reason": self.value.get("reason")}

    def recover(self, store=None):
        """Called only while holding the companion's OS management/runtime locks."""
        result = self._recover(store)
        # Execution references survive response loss and the admission/journal gap.
        # Only verified terminal operations may release their own source protection.
        if self.value["state"] in TERMINAL and self.value["state"] != "recovery_blocked":
            from clytie.backup.previews import PreviewCatalog

            catalog = PreviewCatalog(self.path.parents[4], self.clock)
            try:
                plans = catalog.for_operation(self.value["id"])
                for plan in plans:
                    if plan["kind"] == "memory":
                        catalog.release(self.value["id"])
            except (OSError, ValueError, KeyError):
                # Unknown source authority is not evidence that a dependency ended.
                self.value["backup_release_pending"] = True
                self.save()
        return result

    def _recover(self, store=None):
        if not self.cleanup_temporaries():
            return self.blocked("temporary_unverified")
        if self.value["state"] in TERMINAL:
            return self.public()
        from pathlib import Path

        artifact = self.value.get("file")
        if artifact:
            return self.recover_file(artifact, store)
        database = self.value.get("database")
        if database:
            path = Path(database)
            if not path.is_file() or path.is_symlink() or path.resolve() != path:
                return self.blocked("database_missing")
            try:
                db = self._database(path, store, "ro")
                try:
                    if [tuple(row) for row in db.execute("PRAGMA quick_check")] != [("ok",)]:
                        return self.blocked("database_invalid")
                    rows = db.execute("SELECT data FROM audit").fetchall()
                    for row in rows:
                        entry = json.loads(row[0])
                        if (
                            entry.get("kind") == "management_committed"
                            and entry.get("operation_id") == self.value["id"]
                        ):
                            # A failed snapshot check must not erase a committed database fact.
                            self.value.update(
                                committed=True, result=safe_result(entry.get("result"))
                            )
                            self.save()
                            break
                finally:
                    if store is None:
                        db.close()
            except (OSError, ValueError, sqlite3.Error):
                return self.blocked("database_unverified")
        if not self.snapshot_verified():
            return self.blocked("snapshot_unverified")
        if self.value["committed"]:
            self.committed()
            return self.public()
        if self.value["state"] in {"accepted", "preparing", "transaction"}:
            self.phase("interrupted")
            return self.public()
        return self.blocked("phase_unverified")


def checkpoint():
    journal = current.get()
    if journal is not None:
        journal.checkpoint()


def begin_database(store, phase="transaction"):
    journal = current.get()
    if journal is not None:
        journal.checkpoint()
        journal.database(store.path)
        journal.phase(phase)


def transaction_committed(store, result):
    journal = current.get()
    if journal is not None:
        journal.checkpoint()
        journal.mark_transaction(store, result)


def finish_commit(result):
    journal = current.get()
    if journal is not None:
        journal.committed(result)


def publish_file(path, data, result=None):
    from clytie.plugin_host.snapshots import atomic_write

    journal = current.get()
    if journal is not None:
        journal.prepare_file(path, data, result)
    atomic_write(path, data)
    if journal is not None:
        journal.committed(result)


class ManagementBusy(Exception):
    """A companion already has a management writer or an unresolved operation."""


def gate(home, cid):
    from contextlib import contextmanager

    from clytie.plugin_host.locking import OperationBusy, file_lock

    @contextmanager
    def held():
        path = contained(home, home / "companions" / identifier(cid) / "management/operation.lock")
        try:
            with file_lock(path, {**process_identity(), "operation": "memory_management"}):
                yield
        except OperationBusy:
            raise ManagementBusy() from None

    return held()


def pending(home, cid, clock):
    directory = contained(home, home / "companions" / identifier(cid) / "management/operations")
    block = contained(home, directory.parent / "blocked.json")
    if block.exists():
        raise ManagementBusy()
    if not directory.exists():
        return []
    records = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json":
            continue
        try:
            contained(home, path)
            journal = Journal.load(path, clock)
        except (OSError, ValueError, KeyError, TypeError):
            # Preserve the original evidence; neither a damaged journal nor its later
            # disappearance authorizes a new writer or an empty replacement database.
            write(block, {"code": "management_recovery_blocked", "reason": "journal_unreadable"})
            raise ManagementBusy() from None
        if journal.value["state"] not in TERMINAL or journal.value["state"] == "recovery_blocked":
            records.append(journal)
    return records


def owns_runtime():
    journal = current.get()
    return journal is not None and journal.runtime_owned


def cancellation():
    from clytie.sdk import CancelToken

    journal = current.get()
    return journal.cancel_token if journal is not None else CancelToken()


def executor_alive(identity):
    if (
        not isinstance(identity, dict)
        or not isinstance(identity.get("pid"), int)
        or identity["pid"] <= 0
        or not isinstance(identity.get("started_at"), str)
    ):
        return None
    try:
        return process_identity(identity["pid"]) == identity
    except (FileNotFoundError, ProcessLookupError):
        return False
    except OSError as exc:
        if getattr(exc, "winerror", None) == 87:
            return False
        return None


def recover_locked(home, cid, clock):
    from clytie.memory.transfer import runtime_lock

    with runtime_lock(home, cid, "management_recovery"):
        for journal in pending(home, cid, clock):
            if journal.value["state"] == "recovery_blocked":
                raise ManagementBusy()
            alive = executor_alive(journal.value.get("executor"))
            if alive is True:
                raise ManagementBusy()
            if alive is None:
                journal.blocked("executor_unverified")
                raise ManagementBusy()
            journal.recover()
            if journal.value["state"] == "recovery_blocked":
                raise ManagementBusy()


def recover_pending(home, cid, clock):
    with gate(home, cid):
        recover_locked(home, cid, clock)
