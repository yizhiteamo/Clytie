"""持有记忆运行锁后的格式迁移；先校验完整加密快照再原子建表。"""

import hashlib
import json
import os
import sqlite3
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from clytie.plugin_host.snapshots import UpgradeSnapshots, atomic_write, contained

from .crypto import b64, un64
from .package import temporary_database
from .store import canonical


def migrate(store, home, companion_id, log):
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
        store.db.execute("PRAGMA user_version=2")
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise
