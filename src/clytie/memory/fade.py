"""独立淡化状态；读取投影不改动内容或修订指纹。"""

from datetime import datetime


def eligible(store, record):
    if record.basis != "observed" or (
        record.kind == "episode" and "core:with_owner" in record.tags
    ):
        return False
    if record.kind == "profile" and "core:profile" in record.tags:
        return True
    if record.origin_conversation_id is None:
        return False
    from .origins import binding, resolve

    entry = binding(store, record)
    if entry is None:
        return False
    resolved = resolve(store, record)
    internal = store.config.get("names", {}).get("internal_scenes", ["owner_private"])
    if resolved:
        if resolved[1]["scene"] == "owner_private" or resolved[1]["scene"] in internal:
            return False
    elif entry["category"] in {"owner_private", "internal"}:
        return False
    return record.kind in {"episode", "profile", "jargon"}


def state(store, record):
    config = store.config.get("memory", {}).get("fade", {})
    row = None
    if store.has_table("fade"):
        row = store.db.execute(
            "SELECT * FROM fade WHERE record_id=?", (record.record_id,)
        ).fetchone()
    return (
        datetime.fromisoformat(row["last_recalled_at"]) if row else record.created_at,
        row["half_life_days"] if row else config.get("half_life_days", 30),
    )


def projected(store, record):
    if not eligible(store, record):
        weight = 1.0
    else:
        recalled, half = state(store, record)
        days = max(0, (store.clock.now() - recalled).total_seconds() / 86_400)
        weight = 0.5 ** (days / half)
    threshold = store.config.get("memory", {}).get("fade", {}).get("freeze_below", 0.1)
    return record.model_copy(
        update={
            "decay": record.decay.model_copy(
                update={
                    "weight": weight,
                    "frozen": weight < threshold,
                }
            )
        }
    )


def reinforce(store, records):
    config = store.config.get("memory", {}).get("fade", {})
    rows = []
    for record in records:
        if eligible(store, record):
            _, half = state(store, record)
            rows.append(
                (
                    record.record_id,
                    store.clock.now().isoformat(),
                    min(half * config.get("reinforce", 1.5), config.get("max_half_life_days", 365)),
                )
            )
    if rows:
        with store.transaction():
            store.db.executemany("INSERT OR REPLACE INTO fade VALUES(?,?,?)", rows)
