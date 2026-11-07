"""Instance backup key custody; secret bytes never form a management result."""

import hashlib
import os
from pathlib import Path

from clytie.plugin_host.locking import file_lock, process_identity
from clytie.plugin_host.snapshots import UpgradeSnapshots, contained
from clytie.plugin_host.support import HostLog
from clytie.supervisor.common import read, write


class KeyErrorCode(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class BackupKeys:
    def __init__(self, home, clock, *, repository, mirror=None):
        self.home, self.clock = Path(home).resolve(), clock
        self.repository = Path(repository).resolve()
        self.mirror = Path(mirror).resolve() if mirror else None
        self.path = self.home / "secrets/backup.key"
        self.state = self.home / "supervisor/backup-key.json"

    def _lock(self):
        return file_lock(
            self.home / "supervisor/backup-key.lock",
            {**process_identity(), "operation": "backup_key"},
        )

    def _read(self):
        path = contained(self.home, self.path)
        try:
            value = path.read_bytes()
        except FileNotFoundError:
            raise KeyErrorCode("key_missing") from None
        if len(value) != 32:
            raise KeyErrorCode("key_invalid")
        return value

    def ensure(self):
        with self._lock():
            if not self.path.exists():
                products = self.home / "backups"
                allowed = set()
                root = products / "regular"
                if (root / ".clytie-repository.json").exists():
                    from .repository import INDEX, MARKER, REFERENCES, SourceRepository

                    source = SourceRepository(root, self.clock)
                    index = source.index()
                    if not index["records"] and not index["deletions"]:
                        allowed = {
                            root / name
                            for name in (
                                INDEX,
                                MARKER,
                                REFERENCES,
                                ".clytie-initialize.lock",
                                ".clytie-index.lock",
                                ".clytie-references.lock",
                            )
                        }
                if self.state.exists() or (
                    products.exists()
                    and any(p.is_file() and p not in allowed for p in products.rglob("*"))
                ):
                    raise KeyErrorCode("key_missing")
                UpgradeSnapshots(self.home, self.clock, HostLog())._key(create=True)
            key = self._read()
            fingerprint = hashlib.sha256(key).hexdigest()
            saved = read(self.state, {})
            if saved and saved.get("fingerprint") != fingerprint:
                raise KeyErrorCode("key_changed")
            if not saved:
                write(
                    self.state,
                    dict(
                        fingerprint=fingerprint,
                        acknowledged=False,
                        copy_verified=False,
                        created_at=self.clock.now().isoformat(),
                    ),
                )
            return fingerprint

    def status(self):
        try:
            fingerprint = hashlib.sha256(self._read()).hexdigest()
        except KeyErrorCode as exc:
            return {
                "available": False,
                "code": str(exc),
                "needs_safekeeping": True,
                "acknowledged": False,
                "copy_verified": False,
            }
        saved = read(self.state, {})
        matches = saved.get("fingerprint") == fingerprint
        return dict(
            available=True,
            fingerprint=fingerprint,
            acknowledged=matches and saved.get("acknowledged", False),
            needs_safekeeping=not (matches and saved.get("acknowledged")),
            copy_verified=matches and saved.get("copy_verified", False),
            verified_at=saved.get("verified_at") if matches else None,
        )

    def acknowledge(self):
        with self._lock():
            fingerprint = hashlib.sha256(self._read()).hexdigest()
            state = read(self.state, {})
            if state.get("fingerprint", fingerprint) != fingerprint:
                raise KeyErrorCode("key_changed")
            state.update(
                fingerprint=fingerprint,
                acknowledged=True,
                acknowledged_at=self.clock.now().isoformat(),
            )
            write(self.state, state)
        return self.status()

    def export(self, destination):
        destination = Path(destination)
        # Reject links before resolving; resolving alone would erase evidence.
        for path in (destination, *destination.parents):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise KeyErrorCode("key_export_path_invalid")
        destination = destination.resolve()
        forbidden = [self.home / "backups", self.repository]
        if self.mirror:
            forbidden.append(self.mirror)
        if any(destination.is_relative_to(p) for p in forbidden) or any(
            (p / ".git").exists() for p in destination.parents
        ):
            raise KeyErrorCode("key_export_path_invalid")
        key = self._read()
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise KeyErrorCode("key_export_exists") from None
        with os.fdopen(fd, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        if destination.read_bytes() != key:
            raise KeyErrorCode("key_export_verification_failed")
        return {"fingerprint": hashlib.sha256(key).hexdigest(), "exported": True}

    def verify(self, source):
        # Only this explicit operation reads the user-selected copy. status() does not.
        with self._lock():
            expected = hashlib.sha256(self._read()).hexdigest()
            state = read(self.state, {})
            if state.get("fingerprint", expected) != expected:
                raise KeyErrorCode("key_changed")
            state.update(fingerprint=expected, copy_verified=False)
            try:
                data = Path(source).read_bytes()
                if len(data) != 32 or hashlib.sha256(data).hexdigest() != expected:
                    raise KeyErrorCode("key_mismatch")
            except (OSError, KeyErrorCode):
                write(self.state, state)
                raise
            state.update(copy_verified=True, verified_at=self.clock.now().isoformat())
            write(self.state, state)
        return {"fingerprint": expected, "verified": True}
