"""SQLite 当前记录与追加修订；所有场景读取共用投影。"""

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from clytie.plugin_host.memory import current_conversation, current_scene
from clytie.sdk.context import RevisionConflict
from clytie.sdk.types import MemoryRecord

from . import origins

BASIS = {"observed": 0, "owner_stated": 1, "owner_confirmed": 2}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def normalized(text):
    return unicodedata.normalize("NFKC", text).strip()


def tokens(text):
    result = set()
    for word in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", normalized(text).casefold()):
        result.add(word)
        if "\u3400" <= word[0] <= "\u9fff":
            result.update(word)
            result.update(word[i : i + 2] for i in range(len(word) - 1))
    return result


def query_tokens(text):
    words = tokens(text)
    if re.search(r"[\u3400-\u9fff]{2,}", normalized(text)):
        words = {w for w in words if len(w) > 1 or not "\u3400" <= w <= "\u9fff"}
    return words


def content_hash(record):
    value = record.model_dump(mode="json")
    value.pop("revision")
    return fingerprint(value)


class MemoryStore:
    def __init__(self, path, *, clock, config, instance, readonly=False, transient=False):
        self.path, self.clock = Path(path), clock
        self.config, self.instance = config, instance
        self.changed = None
        self.transient = transient
        if readonly and not transient:
            self.db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            if not transient:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(":memory:" if transient else self.path, isolation_level=None)
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                self.db.close()
                raise ValueError("memory schema too new")
            if version == 0:
                self.db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO metadata VALUES('generation','0');
                CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions(
                    record_id TEXT, revision INTEGER, data TEXT NOT NULL,
                    hash TEXT NOT NULL, parent TEXT, at TEXT NOT NULL,
                    operator TEXT NOT NULL, trace TEXT, import_id TEXT, reason TEXT,
                    PRIMARY KEY(record_id,revision));
                CREATE INDEX IF NOT EXISTS revision_hash ON revisions(record_id,hash);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS keywords(
                    token TEXT, record_id TEXT, PRIMARY KEY(token,record_id));
                CREATE TABLE IF NOT EXISTS vectors(
                    record_id TEXT, revision INTEGER, embedder_id TEXT, vector TEXT,
                    PRIMARY KEY(record_id,revision,embedder_id));
                CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS foreign_revisions(
                    package TEXT, record_id TEXT, revision INTEGER, data TEXT,
                    PRIMARY KEY(package,record_id,revision));
                CREATE TABLE recollect_progress(conversation_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE fade(record_id TEXT PRIMARY KEY, last_recalled_at TEXT NOT NULL, half_life_days REAL NOT NULL);
                CREATE TABLE conversation_registry(key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, channel TEXT NOT NULL, kind TEXT NOT NULL, platform_id TEXT NOT NULL, category TEXT NOT NULL);
                CREATE TABLE revision_origins(record_id TEXT, revision INTEGER, registry_key TEXT NOT NULL, PRIMARY KEY(record_id,revision));
                PRAGMA user_version=2;
            """)
        self.db.row_factory = sqlite3.Row
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 2:
            self.db.close()
            raise ValueError("memory schema too new")

        if self.db.execute("PRAGMA user_version").fetchone()[0] == 2 and any(
            not self.has_table(table) for table in origins.TABLES
        ):
            self.db.close()
            raise ValueError("incomplete memory schema")

    def has_table(self, name):
        return (
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )

    def materialize(self):
        """仅持有运行锁的 apply 调用；临时计划库在这之前不落盘。"""
        if not self.transient:
            return
        if self.path.exists():
            raise ValueError("计划失效，记忆库已建立")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        replacement = sqlite3.connect(self.path, isolation_level=None)
        try:
            self.db.backup(replacement)
            replacement.row_factory = sqlite3.Row
            replacement.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            replacement.close()
            raise
        self.db.close()
        self.db = replacement
        self.transient = False

    def close(self):
        self.db.close()

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, canonical(value)))

    @property
    def uid(self):
        return self.meta("companion_uid")

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.set_meta("generation", self.meta("generation", 0) + 1)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def audit(self, kind, **fields):
        self.db.execute(
            "INSERT INTO audit(data) VALUES(?)",
            (canonical(dict(kind=kind, at=self.clock.now().isoformat(), **fields)),),
        )

    def get_raw(self, record_id):
        row = self.db.execute("SELECT data FROM records WHERE id=?", (record_id,)).fetchone()
        return MemoryRecord.model_validate_json(row[0]) if row else None

    def all_records(self):
        return [
            MemoryRecord.model_validate_json(r[0])
            for r in self.db.execute("SELECT data FROM records ORDER BY id")
        ]

    def visible(self, record, scene, conversation_id, permissions=None):
        if record.retracted or scene == "stranger_private":
            return False
        if permissions is not None:
            if not set(permissions) & {"memory.read.public", "memory.read.private"}:
                return False
            if record.visibility == "private" and "memory.read.private" not in permissions:
                return False
        private = self.config.get("memory", {}).get("private_scenes", ["owner_private"])
        internal = self.config.get("names", {}).get("internal_scenes", ["owner_private"])
        if record.visibility == "private" and scene not in private:
            return False
        if scene in internal:
            return True
        from .presence import present_actors

        if record.kind == "profile" and "core:profile" in record.tags:
            return record.subject in present_actors(conversation_id)
        resolved = origins.resolve(self, record)
        if not resolved:
            return False
        cid, origin = resolved
        if record.kind == "jargon":
            return origins.scope(self, record) == conversation_id
        return origin["scene"] == "owner_private" or (
            origin["kind"] == "group" and cid == conversation_id
        )

    def project(self, scene, conversation_id, permissions=None):
        from .fade import projected

        return [
            projected(self, r)
            for r in self.all_records()
            if self.visible(r, scene, conversation_id, permissions)
        ]

    def _save(
        self,
        record,
        *,
        operator,
        trace=None,
        import_id=None,
        reason=None,
        timestamp=None,
        origin_binding=origins.UNSET,
    ):
        old = self.get_raw(record.record_id)
        parent = content_hash(old) if old else None
        if origin_binding is origins.UNSET:
            origin_binding = (
                origins.binding(self, old)
                if old
                else origins.configured(self, record.origin_conversation_id)
            )
        payload = record.model_dump_json()
        self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?)", (record.record_id, payload))
        self.db.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                record.record_id,
                record.revision,
                payload,
                content_hash(record),
                parent,
                timestamp if timestamp is not None else self.clock.now().isoformat(),
                operator,
                trace,
                import_id,
                reason,
            ),
        )
        origins.attach(self, record, origin_binding)
        self.db.execute("DELETE FROM keywords WHERE record_id=?", (record.record_id,))
        if not record.retracted:
            self.db.executemany(
                "INSERT INTO keywords VALUES(?,?)",
                [(word, record.record_id) for word in sorted(tokens(record.content))],
            )
        self.audit(
            "memory_revision",
            record_id=record.record_id,
            revision=record.revision,
            operator=operator,
            import_id=import_id,
        )
        if self.changed:
            self.changed()
        return record

    def write(
        self,
        draft,
        *,
        scene,
        conversation_id,
        operator="plugin",
        permissions=None,
        owner=None,
        explicit=False,
        trace=None,
    ):
        data = draft.model_dump(mode="json") if hasattr(draft, "model_dump") else dict(draft)
        basis = "owner_confirmed" if owner and explicit else "owner_stated" if owner else "observed"
        now = self.clock.now()
        fields = {
            k: data[k]
            for k in ("kind", "content", "visibility", "subject", "scope", "tags")
            if k in data
        }
        fields["tags"] = [tag for tag in fields.get("tags", []) if not tag.startswith("core:")]
        if fields.get("kind") == "jargon":
            fields["scope"] = conversation_id
        fields.update(
            record_id=uuid4().hex,
            revision=1,
            basis=basis,
            write_mode="confirmed" if basis == "owner_confirmed" else "auto",
            origin_conversation_id=conversation_id,
            source_ref=trace or data.get("source_ref"),
            source_type="private"
            if scene == "owner_private"
            else "owner_group_interaction"
            if owner
            else "group",
            created_at=now,
            updated_at=now,
            retracted=False,
            decay={"weight": 1, "frozen": False},
        )
        if owner:
            fields["subject"] = owner
        new = MemoryRecord.model_validate(fields)
        with self.transaction():
            for old in self.project(scene, conversation_id, permissions):
                if (
                    all(
                        getattr(old, k) == getattr(new, k)
                        for k in ("kind", "subject", "visibility", "origin_conversation_id")
                    )
                    and origins.identity(origins.binding(self, old))
                    == origins.identity(origins.configured(self, conversation_id))
                    and normalized(old.content) == normalized(new.content)
                ):
                    old = self.get_raw(old.record_id)
                    if owner and explicit and old.basis != "owner_confirmed":
                        old = old.model_copy(
                            update={
                                "revision": old.revision + 1,
                                "basis": "owner_confirmed",
                                "write_mode": "confirmed",
                                "updated_at": now,
                            }
                        )
                        return self._save(old, operator=operator, trace=trace)
                    return old
            if not self.uid:
                self.set_meta("companion_uid", uuid4().hex)
            return self._save(new, operator=operator, trace=trace)

    def revise(
        self,
        record_id,
        revision,
        changes,
        *,
        scene,
        conversation_id,
        permissions=None,
        operator="plugin",
        explicit=False,
        trace=None,
        reason=None,
    ):
        with self.transaction():
            old = self.get_raw(record_id)
            if old is None or not self.visible(old, scene, conversation_id, permissions):
                raise KeyError("record unavailable")
            if operator == "plugin" and old.basis != "observed":
                raise KeyError("record unavailable")
            if old.revision != revision:
                raise RevisionConflict("memory revision changed")
            allowed = {"content", "visibility", "tags", "retracted", "kind"}
            update = {k: v for k, v in dict(changes).items() if k in allowed}
            if "tags" in update:
                update["tags"] = [tag for tag in update["tags"] if not tag.startswith("core:")] + [
                    tag for tag in old.tags if tag.startswith("core:")
                ]
            update.update(revision=old.revision + 1, updated_at=self.clock.now())
            if explicit and operator == "owner":
                update.update(basis="owner_confirmed", write_mode="confirmed")
            new = MemoryRecord.model_validate({**old.model_dump(), **update})
            return self._save(new, operator=operator, trace=trace, reason=reason)

    def for_permissions(self, permissions):
        return PluginMemory(self, frozenset(permissions))


class PluginMemory:
    def __init__(self, store, permissions):
        self.store, self.permissions = store, permissions

    def scope(self):
        return dict(
            scene=current_scene(),
            conversation_id=current_conversation(),
            permissions=self.permissions,
        )

    async def search(self, text, *, limit=10):
        words = tokens(text)
        records = self.store.project(**self.scope())
        return sorted(
            (r for r in records if not r.decay.frozen and (not words or words & tokens(r.content))),
            key=lambda r: (-len(words & tokens(r.content)), r.record_id),
        )[:limit]

    async def get(self, record_id):
        record = self.store.get_raw(record_id)
        return record if record and self.store.visible(record, **self.scope()) else None

    async def write(self, draft):
        return self.store.write(draft, **self.scope())

    async def revise(self, record_id, expected_revision, changes):
        return self.store.revise(record_id, expected_revision, changes, **self.scope())

    async def retract(self, record_id, expected_revision, reason):
        return self.store.revise(
            record_id, expected_revision, {"retracted": True}, reason=reason, **self.scope()
        )
