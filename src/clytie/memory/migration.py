"""持有记忆运行锁后的格式迁移；先校验完整加密快照再原子建表。"""

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from clytie.plugin_host.snapshots import UpgradeSnapshots, atomic_write, contained

from .crypto import b64, un64
from .errors import MemoryStartupError
from .package import temporary_database
from .store import canonical


def migrate(store, home, companion_id, log):
    try:
        _migrate(store, home, companion_id, log)
    except MemoryStartupError:
        raise
    except Exception as exc:
        raise MemoryStartupError("memory migration blocked") from exc


def _migrate(store, home, companion_id, log):
    version = store.db.execute("PRAGMA user_version").fetchone()[0]
    if version == 2:
        return
    if version != 1:
        raise ValueError("unsupported memory schema")
    from clytie.plugin_host.support import HostLog

    key = UpgradeSnapshots(home, store.clock, log or HostLog())._key(create=True)
    header = dict(kind="memory_full", schema_version=1, fingerprint=hashlib.sha256(key).hexdigest())
    with temporary_database(home, companion_id, "migration") as path:
        db = sqlite3.connect(path)
        try:
            store.db.backup(db)
        finally:
            db.close()
        raw = path.read_bytes()
        nonce = os.urandom(12)
        encrypted = AESGCM(key).encrypt(nonce, raw, canonical(header).encode())
        target = contained(
            home,
            home
            / "backups"
            / "memory"
            / companion_id
            / (
                "pre-migrate-"
                + store.clock.now().strftime("%Y%m%dT%H%M%S%fZ")
                + "-"
                + uuid4().hex
                + ".enc"
            ),
        )
        atomic_write(
            target, canonical(dict(header=header, nonce=b64(nonce), data=b64(encrypted))).encode()
        )
        saved = json.loads(target.read_bytes())
        checked = AESGCM(key).decrypt(
            un64(saved["nonce"]), un64(saved["data"]), canonical(saved["header"]).encode()
        )
        if checked != raw:
            raise ValueError("memory migration snapshot mismatch")
        path.write_bytes(checked)
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            if (
                db.execute("PRAGMA quick_check").fetchall() != [("ok",)]
                or db.execute("PRAGMA user_version").fetchone()[0] != 1
            ):
                raise ValueError("memory migration snapshot invalid")
        finally:
            db.close()
    store.db.execute("BEGIN IMMEDIATE")
    try:
        store.db.execute(
            "CREATE TABLE recollect_progress(conversation_id TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        store.db.execute(
            "CREATE TABLE fade(record_id TEXT PRIMARY KEY, last_recalled_at TEXT NOT NULL, half_life_days REAL NOT NULL)"
        )
        from .origins import TABLES, backfill

        for table, columns in TABLES.items():
            store.db.execute(f"CREATE TABLE {table}({columns})")
        backfill(store)
        store.db.execute("PRAGMA user_version=2")
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise
    _record_success(home, companion_id, target, store.clock, log or HostLog())


def _record_success(home, companion_id, target, clock, log):
    """Only a durable successful migration receipt authorizes retention."""
    directory = contained(home, Path(home) / "backups/memory" / companion_id)
    index_path = contained(home, directory / "migration-receipts.json")
    try:
        index = (
            json.loads(index_path.read_bytes())
            if index_path.exists()
            else {"schema_version": 1, "snapshots": []}
        )
        if index.get("schema_version") != 1 or not isinstance(index.get("snapshots"), list):
            raise ValueError("migration_receipts_invalid")
        info = target.stat()
        index["snapshots"].append(
            dict(
                path=target.name,
                sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                device=info.st_dev,
                inode=info.st_ino,
                size=info.st_size,
                verified_success_at=clock.now().isoformat(),
            )
        )
        atomic_write(index_path, canonical(index).encode())
        management = contained(home, Path(home) / "companions" / companion_id / "management")
        if (management / "blocked.json").exists():
            log.warning("memory.migration_retention_protected")
            return
        from clytie.api.management_journal import Journal

        operations = management / "operations"
        if operations.exists():
            for path in operations.glob("*.json"):
                journal = Journal.load(contained(home, path), clock)
                if journal.value["state"] not in {
                    "committed",
                    "cancelled",
                    "interrupted",
                    "failed",
                }:
                    log.warning("memory.migration_retention_protected")
                    return
        from clytie.backup.restore import read_state

        restore = read_state(home)
        if restore and not restore.get("switched"):
            log.warning("memory.migration_retention_protected")
            return
        for row in list(index["snapshots"][:-3]):
            name = row["path"]
            if (
                Path(name).name != name
                or not name.startswith("pre-migrate-")
                or not name.endswith(".enc")
            ):
                raise ValueError("migration_receipt_path_invalid")
            path = contained(home, directory / name)
            info = path.stat()
            if (info.st_dev, info.st_ino, info.st_size) != (
                row["device"],
                row["inode"],
                row["size"],
            ) or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError("migration_snapshot_changed")
            path.unlink()
            index["snapshots"].remove(row)
            atomic_write(index_path, canonical(index).encode())
        known = {row["path"] for row in index["snapshots"]}
        if any(p.name not in known for p in directory.glob("pre-migrate-*.enc")):
            log.warning("memory.migration_unregistered_snapshots")
    except (OSError, ValueError, KeyError, TypeError):
        # Migration has already committed. Preserve all uncertain files and surface
        # maintenance failure without falsely reporting a rolled-back schema.
        log.warning("memory.migration_retention_incomplete")
