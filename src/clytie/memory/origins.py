"""本体维护不可变来源登记；投影永远按三项平台身份解析。"""

FIELDS = ("conversation_id", "channel", "kind", "platform_id", "category")
TABLES = {
    "conversation_registry": "key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, channel TEXT NOT NULL, kind TEXT NOT NULL, platform_id TEXT NOT NULL, category TEXT NOT NULL",
    "revision_origins": "record_id TEXT, revision INTEGER, registry_key TEXT NOT NULL, PRIMARY KEY(record_id,revision)",
}
UNSET = object()


def configured(store, cid):
    entry = store.instance.get("conversations", {}).get(cid)
    if not entry or not all(
        isinstance(entry.get(k), str) and entry[k] for k in ("channel", "kind", "platform_id")
    ):
        return None
    internal = store.config.get("names", {}).get("internal_scenes", ["owner_private"])
    return dict(
        conversation_id=cid,
        channel=entry["channel"],
        kind=entry["kind"],
        platform_id=entry["platform_id"],
        category="owner_private"
        if entry["scene"] == "owner_private"
        else "internal"
        if entry["scene"] in internal
        else "group",
    )


def binding(store, record):
    if record is None or record.origin_conversation_id is None:
        return None
    if not store.has_table("revision_origins"):
        # 未迁移的本地旧库只读兼容；导入旧包不会走这条路径。
        return configured(store, record.origin_conversation_id)
    row = store.db.execute(
        "SELECT r.* FROM conversation_registry r JOIN revision_origins o ON o.registry_key=r.key WHERE o.record_id=? AND o.revision=?",
        (record.record_id, record.revision),
    ).fetchone()
    return {k: row[k] for k in FIELDS} if row else None


def identity(entry):
    return tuple(entry[k] for k in ("channel", "kind", "platform_id")) if entry else None


def attach(store, record, entry):
    from .store import fingerprint

    if not entry or record.origin_conversation_id is None:
        return
    if entry["conversation_id"] != record.origin_conversation_id:
        raise ValueError("origin registration does not match revision")
    key = fingerprint(entry)
    store.db.execute(
        "INSERT OR IGNORE INTO conversation_registry VALUES(?,?,?,?,?,?)",
        (key, *(entry[k] for k in FIELDS)),
    )
    store.db.execute(
        "INSERT INTO revision_origins VALUES(?,?,?)", (record.record_id, record.revision, key)
    )


def resolve(store, record):
    entry = binding(store, record)
    if not entry:
        return None
    found = [
        (cid, c)
        for cid, c in store.instance.get("conversations", {}).items()
        if identity(c) == identity(entry)
    ]
    return found[0] if len(found) == 1 else None


def scope(store, record):
    if record.scope != record.origin_conversation_id:
        return None
    resolved = resolve(store, record)
    return resolved[0] if resolved else None


def backfill(store):
    from clytie.sdk.types import MemoryRecord

    for row in store.db.execute("SELECT data FROM revisions").fetchall():
        record = MemoryRecord.model_validate_json(row[0])
        attach(store, record, configured(store, record.origin_conversation_id))
