"""A3.3 模式迁移快照来自临时数据库，使用临时生成的密钥。"""

import json
import sqlite3

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from clytie.memory.crypto import un64
from clytie.memory.migration import migrate
from clytie.memory.store import MemoryStore, canonical, content_hash
from clytie.sdk.testing import MockClock


def test_legacy_schema_snapshot_preserves_revisions_and_roundtrip(tmp_path):
    path = tmp_path / "companions/companion_a/memory/memory.sqlite3"
    store = MemoryStore(path, clock=MockClock(), config={}, instance={})
    record = store.write(
        dict(kind="episode", content="虚构旧版记录", visibility="private"),
        scene="owner_private",
        conversation_id="owner_chat",
    )
    original = content_hash(record)
    store.db.executescript("DROP TABLE fade; DROP TABLE recollect_progress; PRAGMA user_version=1;")
    store.close()
    store = MemoryStore(path, clock=MockClock(), config={}, instance={})
    assert not store.has_table("fade")
    try:
        migrate(store, tmp_path, "companion_a", None)
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert content_hash(store.get_raw(record.record_id)) == original
        files = list((tmp_path / "backups/memory/companion_a").glob("*.enc"))
        assert len(files) == 1
        envelope = json.loads(files[0].read_bytes())
        raw = AESGCM((tmp_path / "secrets/backup.key").read_bytes()).decrypt(
            un64(envelope["nonce"]), un64(envelope["data"]), canonical(envelope["header"]).encode()
        )
        restored = tmp_path / "verified-snapshot.sqlite3"
        restored.write_bytes(raw)
        with sqlite3.connect(restored) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 1
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1
        migrate(store, tmp_path, "companion_a", None)
        assert len(list((tmp_path / "backups/memory/companion_a").glob("*.enc"))) == 1
    finally:
        store.close()
