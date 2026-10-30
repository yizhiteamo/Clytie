"""回想提交边界：快照投影、证据核验与原子进度。"""

from uuid import uuid4

from clytie.sdk.types import MemoryRecord

from .store import canonical, normalized


class SnapshotReader:
    def __init__(self, records):
        self.rows = tuple(
            sorted((r for r in records if not r.decay.frozen), key=lambda r: r.record_id)
        )
        self.labels = {f"[记忆{i}]": r for i, r in enumerate(self.rows, 1)}

    async def iter(self, *, kinds=None, scope=None, updated_after=None, batch=200):
        if batch < 1:
            raise ValueError("batch must be positive")
        rows = [
            r
            for r in self.rows
            if (kinds is None or r.kind in kinds)
            and (scope is None or r.scope == scope)
            and (updated_after is None or r.updated_at > updated_after)
        ]
        for start in range(0, len(rows), batch):
            yield rows[start : start + batch]

    async def get(self, record_ids):
        return [r for r in self.rows if r.record_id in record_ids]

    async def count(self):
        return len(self.rows)


def partition(record):
    return (
        record.kind,
        record.subject,
        record.visibility,
        record.origin_conversation_id,
        record.scope,
        tuple(sorted(t for t in record.tags if t.startswith("core:"))),
    )


class RecollectionCommit:
    def __init__(self, store):
        self.store = store

    def candidate(self, item, window, cid, actors, trace):
        group = window.conversation_kind == "group"
        allowed = (
            {"episode", "profile", "jargon"}
            if group
            else {"episode", "event", "fact", "preference"}
        )
        messages = {m.number: m for m in window.messages}
        if (
            item.kind not in allowed
            or not item.content.strip()
            or len(item.content) > (200 if item.kind == "profile" else 300)
        ):
            raise ValueError("invalid_content")
        if not item.evidence or any(i not in messages for i in item.evidence):
            raise ValueError("invalid_evidence")
        evidence = [messages[i] for i in item.evidence]
        owner_labels = {m.speaker for m in window.messages if m.role == "owner"}
        owner_labels.update(
            label
            for label, actor in actors.items()
            if self.store.instance.get("people", {}).get(actor, {}).get("role") == "owner"
        )

        def involves(m, roles):
            labels = {n.speaker for n in window.messages if n.role in roles}
            if "owner" in roles:
                labels.update(owner_labels)
            if "self" in roles:
                labels.update(label for label, actor in actors.items() if actor == "self")
            return (
                m.role in roles
                or bool(labels.intersection(m.mentions))
                or (m.reply_to in messages and messages[m.reply_to].role in roles)
            )

        owner = any(involves(m, {"owner"}) for m in evidence)
        if item.with_owner and not owner:
            raise ValueError("invalid_owner_evidence")
        if (
            group
            and item.kind == "episode"
            and not any(involves(m, {"owner", "self"}) for m in evidence)
        ):
            raise ValueError("unrelated_episode")
        if item.kind == "profile":
            if (
                item.about not in actors
                or item.about in owner_labels
                or not any(
                    m.speaker == item.about and m.role not in {"owner", "self"} for m in evidence
                )
            ):
                raise ValueError("invalid_profile_evidence")
        elif item.about is not None:
            raise ValueError("unexpected_about")
        config = self.store.config.get("memory", {}).get("recollect", {})
        now = self.store.clock.now()
        return MemoryRecord(
            record_id=uuid4().hex,
            revision=1,
            kind=item.kind,
            content=item.content,
            subject=actors[item.about] if item.kind == "profile" else None,
            visibility=item.visibility
            or (config.get("group_visibility", "relationship") if group else "private"),
            write_mode="auto",
            basis="observed",
            origin_conversation_id=None if item.kind == "profile" else cid,
            source_type="owner_group_interaction" if group else "private",
            source_ref=trace,
            scope=cid if item.kind == "jargon" else None,
            created_at=now,
            updated_at=now,
            decay={"weight": 1, "frozen": False},
            tags=["core:profile"]
            if item.kind == "profile"
            else ["core:with_owner"]
            if group and owner
            else [],
            retracted=False,
        )

    def commit(self, items, window, reader, *, cid, actors, trace, progress, cancel, rejected=()):
        store = self.store
        written = dropped = 0
        cancel.raise_if_cancelled()
        with store.transaction():
            for index, item in enumerate(items):
                if index in rejected or index >= store.config.get("memory", {}).get(
                    "recollect", {}
                ).get("max_items", 8):
                    dropped += 1
                    continue
                try:
                    record = self.candidate(item, window, cid, actors, trace)
                    merged = []
                    for label in item.merges:
                        old = reader.labels.get(label)
                        current = store.get_raw(old.record_id) if old else None
                        if (
                            not current
                            or current.revision != old.revision
                            or current.basis != "observed"
                            or not store.visible(current, window.scene, cid)
                            or partition(current) != partition(record)
                        ):
                            raise ValueError("invalid_merge")
                        merged.append(current)
                    if len({r.record_id for r in merged}) != len(merged):
                        raise ValueError("duplicate_merge")
                except ValueError:
                    dropped += 1
                    continue
                visible = store.project(window.scene, cid)
                duplicate = next(
                    (
                        r
                        for r in visible
                        if (r.kind, r.subject, r.visibility, r.origin_conversation_id)
                        == (
                            record.kind,
                            record.subject,
                            record.visibility,
                            record.origin_conversation_id,
                        )
                        and normalized(r.content) == normalized(record.content)
                    ),
                    None,
                )
                if duplicate and not merged:
                    continue
                if record.kind == "profile" and not merged:
                    merged = [
                        store.get_raw(r.record_id)
                        for r in visible
                        if r.kind == "profile"
                        and r.subject == record.subject
                        and r.basis == "observed"
                    ]
                if merged:
                    merged.sort(key=lambda r: (r.created_at, r.record_id))
                    first = merged[0]
                    record = record.model_copy(
                        update={
                            "record_id": first.record_id,
                            "revision": first.revision + 1,
                            "created_at": first.created_at,
                        }
                    )
                    for old in merged[1:]:
                        store._save(
                            old.model_copy(
                                update={
                                    "revision": old.revision + 1,
                                    "retracted": True,
                                    "updated_at": store.clock.now(),
                                }
                            ),
                            operator="core:recollect",
                            reason="并入另一条记忆",
                        )
                if not store.uid:
                    store.set_meta("companion_uid", uuid4().hex)
                store._save(record, operator="core:recollect", trace=trace)
                written += 1
            cancel.raise_if_cancelled()
            progress = dict(
                progress, at=store.clock.now().isoformat(), written=written, dropped=dropped
            )
            store.db.execute(
                "INSERT OR REPLACE INTO recollect_progress VALUES(?,?)", (cid, canonical(progress))
            )
            store.audit("memory.recollect", written=written, dropped=dropped)
        return progress
