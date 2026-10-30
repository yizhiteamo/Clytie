"""仅从持久化基线之后的账本构造回想窗口。"""

import json
from datetime import datetime

from clytie.sdk.types import RecollectMessage, RecollectRemembered, RecollectWindow


def highwater(ledger):
    return {
        name: ledger.db.execute(f'SELECT coalesce(max(rowid),0) FROM "{name}"').fetchone()[0]
        for name in ("inbound", "intent")
    }


def fresh_rows(ledger, progress, cid):
    rows = []
    end = dict(progress)
    for table in ("inbound", "intent"):
        for number, payload in ledger.db.execute(
            f'SELECT rowid,data FROM "{table}" WHERE rowid>? ORDER BY rowid',
            (progress.get(table, 0),),
        ):
            row = json.loads(payload)
            if row.get("conversation_id") != cid:
                continue
            if table == "intent" and row.get("status") not in {"sent", "failed", "unknown"}:
                break
            end[table] = number
            if table == "inbound":
                if not row.get("trace_id"):
                    row["trace_id"] = next(
                        (
                            r["trace_id"]
                            for r in ledger.rows("record")
                            if r.get("source_event_id") == row.get("event_id")
                        ),
                        None,
                    )
                msg = row.get("message")
                if (
                    not msg
                    or row.get("reason") == "boundary_paused"
                    or row.get("state") == "dropped_on_restart"
                ):
                    continue
                at = row.get("content_written_at") or msg["sent_at"]
            else:
                if (
                    row.get("status_reaction")
                    or row.get("kind") == "react"
                    or row.get("status") == "failed"
                ):
                    continue
                at = row["created_at"]
            rows.append((datetime.fromisoformat(at), table, number, row))
    inbound = [r for r in rows if r[1] == "inbound"]
    intents = [r for r in rows if r[1] == "intent"]
    merged, left, right = [], 0, 0
    while left < len(inbound) or right < len(intents):
        if right == len(intents) or (left < len(inbound) and inbound[left][0] <= intents[right][0]):
            merged.append(inbound[left])
            left += 1
        else:
            merged.append(intents[right])
            right += 1
    return merged, end


def build_window(rows, conversation, reader, store):
    labels, actors, messages, refs, traces = {}, {}, [], {}, []

    def label(actor):
        if actor not in labels:
            name = f"speaker_{len(labels) + 1}"
            labels[actor] = name
            actors[name] = actor
        return labels[actor]

    for _, table, _, row in rows:
        if table == "inbound":
            msg = row["message"]
            actor = msg["sender"]
            speaker = label(actor["actor_id"])
            role = actor["role"]
            role = role if role in {"owner", "peer", "member"} else "member"
            parts = msg.get("parts") or []
            reply_to = refs.get(json.dumps(msg.get("reply_to"), sort_keys=True))
            refs[json.dumps(msg["ref"], sort_keys=True)] = len(messages) + 1
            trace = row.get("trace_id")
        else:
            speaker, role, reply_to = label("self"), "self", None
            parts = row.get("parts") or []
            trace = row.get("trace_id")
        if trace:
            traces.append(trace)
        text = "".join(part.get("text", "") for part in parts if part.get("type") == "text")
        if table == "intent" and row["status"] == "unknown":
            text = "（是否发出不确定）" + text
        mentions = [
            label("self" if part.get("is_self") else part["actor_id"])
            for part in parts
            if part.get("type") == "mention"
        ]
        messages.append(
            RecollectMessage(
                number=len(messages) + 1,
                speaker=speaker,
                role=role,
                text=text,
                reply_to=reply_to,
                mentions=mentions,
            )
        )
    changed = set()
    for trace in traces:
        changed.update(
            row[0]
            for row in store.db.execute(
                "SELECT record_id FROM revisions WHERE trace=? AND operator='owner'", (trace,)
            )
        )
    retracted = [
        r for rid in sorted(changed) if (r := store.get_raw(rid)) is not None and r.retracted
    ]
    remembered = [
        RecollectRemembered(label=key, kind=r.kind, content=r.content, visibility=r.visibility)
        for key, r in reader.labels.items()
        if r.record_id in changed
    ]
    return (
        RecollectWindow(
            scene=conversation.scene,
            conversation_kind=conversation.kind,
            messages=messages,
            remembered=remembered,
            retracted_count=len(retracted),
        ),
        actors,
        retracted,
        "..".join([traces[0], traces[-1]]) if traces else None,
    )


def involved(window, actors, instance):
    labels = {m.speaker for m in window.messages if m.role in {"owner", "self"}}
    labels.update(
        label
        for label, actor in actors.items()
        if actor == "self" or instance.get("people", {}).get(actor, {}).get("role") == "owner"
    )
    return any(
        m.role in {"owner", "self"} or bool(labels.intersection(m.mentions))
        for m in window.messages
    )


def latest_activity(ledger, progress, cid):
    times = []
    attempts = ledger.rows("attempt")
    for table in ("inbound", "intent"):
        for (payload,) in ledger.db.execute(
            f'SELECT data FROM "{table}" WHERE rowid>?', (progress.get(table, 0),)
        ):
            row = json.loads(payload)
            if row.get("conversation_id") != cid:
                continue
            if table == "inbound":
                times.append(row["content_written_at"])
            else:
                times.append(row["created_at"])
                for attempt in attempts:
                    if attempt["intent_id"] == row["intent_id"]:
                        times.append(attempt["started_at"])
                        if attempt.get("receipt_written_at"):
                            times.append(attempt["receipt_written_at"])
    return max(datetime.fromisoformat(at) for at in times)
