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


def sourced(store):
    return [
        r
        for r in store.all_records()
        if not r.retracted
        and r.origin_conversation_id is not None
        and not (r.kind == "profile" and "core:profile" in r.tags)
    ]


def current_key(store, record):
    if not store.has_table("revision_origins"):
        return None
    row = store.db.execute(
        "SELECT registry_key FROM revision_origins WHERE record_id=? AND revision=?",
        (record.record_id, record.revision),
    ).fetchone()
    return row[0] if row else None


def missing_history(store):
    return {
        tuple(pair)
        for pair in store.meta("missing_origins", [])
        if isinstance(pair, list)
        and len(pair) == 2
        and all(isinstance(value, str) for value in pair)
    }


def track(store):
    """在运行/导入锁内观察来源；只在本地集合变化时写库。"""
    records = sourced(store)
    current = {r.record_id: current_key(store, r) for r in records}
    pairs = {(rid, key) for rid, key in missing_history(store) if current.get(rid) == key}
    pairs.update(
        (r.record_id, current[r.record_id])
        for r in records
        if current[r.record_id] is not None and resolve(store, r) is None
    )
    value = [list(pair) for pair in sorted(pairs)]
    if value == store.meta("missing_origins", []):
        return
    if store.db.in_transaction:
        store.set_meta("missing_origins", value)
    else:
        with store.transaction():
            store.set_meta("missing_origins", value)
