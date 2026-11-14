"""Local physical backup repositories and explicit, metadata-only source binding."""

from __future__ import annotations

import json
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from clytie.plugin_host.locking import OperationBusy, file_lock
from clytie.plugin_host.snapshots import atomic_read, atomic_write, canonical

from .container import _safe

MARKER = ".clytie-repository.json"
INDEX = ".clytie-index.json"
REFERENCES = ".clytie-references.json"


class RepositoryError(ValueError):
    def __init__(self, code, *, binding=None):
        self.code, self.binding = code, binding
        super().__init__(code)


def local_path(path):
    path = Path(path)
    if str(path).startswith(("\\\\", "//")) or not path.is_absolute():
        raise RepositoryError("source_repository_unsupported")
    _safe(path)
    if os.name == "nt":
        import ctypes

        if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) not in (2, 3):
            raise RepositoryError("source_repository_unsupported")
    elif os.name == "posix":
        # Linux mount metadata is local OS evidence; unknown mount types fail closed.
        mounts = Path("/proc/self/mountinfo")
        if not mounts.exists():
            raise RepositoryError("source_repository_unsupported")
        choices = []
        for line in mounts.read_text().splitlines():
            left, right = line.split(" - ", 1)
            mount = Path(left.split()[4].replace("\\040", " "))
            if path.is_relative_to(mount):
                choices.append((len(str(mount)), right.split()[0]))
        if not choices or max(choices)[1] not in {
            "ext2",
            "ext3",
            "ext4",
            "xfs",
            "btrfs",
            "tmpfs",
            "overlay",
            "zfs",
        }:
            raise RepositoryError("source_repository_unsupported")
    else:
        raise RepositoryError("source_repository_unsupported")
    return path.resolve()


def physical_identity(path):
    _safe(Path(path))
    value = Path(path).stat()
    if not value.st_ino:
        raise RepositoryError("source_repository_unsupported")
    return {"device": value.st_dev, "inode": value.st_ino}


@contextmanager
def repository_lock(path, operation):
    for attempt in range(1001):
        manager = file_lock(path, {"operation": operation})
        try:
            manager.__enter__()
            break
        except OperationBusy:
            if attempt == 1000:
                raise RepositoryError("reference_busy") from None
            threading.Event().wait(0.01)
    try:
        yield
    finally:
        manager.__exit__(None, None, None)


def holder_identity(home):
    path = Path(home) / "supervisor/backup-holder.json"
    with repository_lock(path.with_suffix(".lock"), "backup_holder"):
        if path.exists():
            data = json.loads(atomic_read(path))
            if data.get("home_identity") != physical_identity(home) or not data.get("instance_id"):
                raise RepositoryError("holder_identity_mismatch")
        else:
            data = {"instance_id": uuid4().hex, "home_identity": physical_identity(home)}
            atomic_write(path, canonical(data))
    return data["instance_id"]


class SourceRepository:
    identity = staticmethod(physical_identity)

    def __init__(self, root, clock, *, expected_id=None):
        self.root, self.clock = local_path(root), clock
        try:
            marker = json.loads(atomic_read(self.root / MARKER))
            if (
                marker["schema_version"] != 1
                or marker["root_identity"] != physical_identity(self.root)
                or marker["root"] != str(self.root)
                or not marker["repository_id"]
            ):
                raise RepositoryError("source_repository_identity_mismatch")
            if expected_id is not None and marker["repository_id"] != expected_id:
                raise RepositoryError("source_repository_identity_mismatch")
            self.marker = marker
            self.repository_id = marker["repository_id"]
        except RepositoryError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RepositoryError("source_repository_unavailable") from exc

    @classmethod
    def initialize(cls, root, clock, *, owner_instance_id, migration_complete=True):
        root = Path(root).absolute()
        local_path(root)
        root.mkdir(parents=True, exist_ok=True)
        with file_lock(root / ".clytie-initialize.lock", {"operation": "repository_initialize"}):
            path = root / MARKER
            if not path.exists():
                marker = {
                    "schema_version": 1,
                    "repository_id": uuid4().hex,
                    "root": str(root.resolve()),
                    "root_identity": physical_identity(root),
                    "owner_instance_id": owner_instance_id,
                    "migration_complete": migration_complete,
                }
                atomic_write(path, canonical(marker))
                atomic_write(
                    root / INDEX, canonical({"schema_version": 1, "records": {}, "deletions": []})
                )
                atomic_write(
                    root / REFERENCES,
                    canonical({"schema_version": 1, "previews": {}, "references": {}}),
                )
            repo = cls(root, clock)
            if repo.marker["owner_instance_id"] != owner_instance_id:
                raise RepositoryError("source_repository_identity_mismatch")
            return repo

    def revalidate(self):
        current = SourceRepository(self.root, self.clock, expected_id=self.repository_id)
        if current.marker != self.marker:
            raise RepositoryError("source_repository_identity_mismatch")

    def index(self):
        self.revalidate()
        try:
            value = json.loads(atomic_read(self.root / INDEX))
            if value["schema_version"] != 1 or not isinstance(value["records"], dict):
                raise ValueError()
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RepositoryError("source_repository_index_invalid") from exc

    def resolve(self, backup_id, *, source=None):
        self.revalidate()
        index = self.index()
        if any(
            row.get("backup_id") == backup_id and row.get("state") == "deleting"
            for row in index["deletions"]
        ):
            raise RepositoryError("source_repository_retiring")
        record = index["records"].get(backup_id)
        if not record:
            raise RepositoryError("source_missing")
        path = local_path(record["path"])
        if not path.is_relative_to(self.root) or path.parent != self.root:
            raise RepositoryError("source_repository_ownership_mismatch")
        try:
            info = path.stat()
        except FileNotFoundError:
            raise RepositoryError("source_missing") from None
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or physical_identity(path) != record["identity"]
        ):
            raise RepositoryError("source_repository_identity_mismatch")
        if source is not None and local_path(source) != path:
            raise RepositoryError("source_repository_ownership_mismatch")
        return {
            "root": str(self.root),
            "repository_id": self.repository_id,
            "backup_id": backup_id,
            "root_identity": physical_identity(self.root),
            "source": str(path),
            "source_identity": physical_identity(path),
        }

    def binding(self, request, *, confirmed, source=None):
        result = self.resolve(request["backup_id"], source=source)
        expected = request.get("repository_id")
        if expected is not None and expected != self.repository_id:
            raise RepositoryError("source_repository_identity_mismatch")
        if not confirmed or not expected:
            raise RepositoryError("source_repository_confirmation_required", binding=result)
        return result

    def register(self, record):
        with file_lock(self.root / ".clytie-index.lock", {"operation": "repository_index"}):
            data = self.index()
            old = data["records"].get(record["backup_id"])
            if old is not None and old != record:
                raise RepositoryError("backup_identity_conflict")
            data["records"][record["backup_id"]] = record
            atomic_write(self.root / INDEX, canonical(data))

    def remove_record(self, backup_id):
        """Caller holds deletion protection and has verified the physical unlink."""
        with repository_lock(self.root / ".clytie-index.lock", "repository_index"):
            data = self.index()
            row = data["records"].get(backup_id)
            if row is not None:
                if Path(row["path"]).exists():
                    raise RepositoryError("backup_evidence_mismatch")
                del data["records"][backup_id]
                atomic_write(self.root / INDEX, canonical(data))

    def catalog(self, holder_instance_id):
        from .previews import _LocalCatalog

        catalog = _LocalCatalog(self.root, self.clock)
        catalog.path = self.root / REFERENCES
        catalog.holder_instance_id = holder_instance_id
        catalog.repository = self
        return catalog

    def retire(self, operation_id):
        """Block new readers before a separately authorized old-root cleanup."""
        with self.catalog(None)._transaction() as data:
            previous = data.get("repository_retirement")
            if previous and previous != operation_id:
                raise RepositoryError("reference_busy")
            if data["references"] or not self.marker.get("migration_complete"):
                raise RepositoryError("reference_recovery_blocked")
            data["repository_retirement"] = operation_id

    def unretire(self, operation_id):
        """Only the same stopped cleanup may lift its admission block."""
        with self.catalog(None)._transaction() as data:
            if data.get("repository_retirement") != operation_id:
                raise RepositoryError("reference_mismatch")
            del data["repository_retirement"]

    @contextmanager
    def deletion(self, source):
        with self.catalog(None).deletion(source) as allowed:
            yield allowed and self.marker.get("migration_complete") is True
