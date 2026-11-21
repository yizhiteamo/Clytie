"""本体从已准入的消息与同会话收据计算称呼、焦点和复核范围。"""

import json

from clytie.sdk.types import Addressed, EventEnvelope


class ConversationMessages:
    """Methods run on the ledger owner; only message snapshots cross the queue."""

    def __init__(self, ledger, names):
        self.ledger = ledger
        self.names = names

    def _sent_at(self, ref):
        row = self.ledger.db.execute(
            """SELECT json_extract(a.data,'$.receipt.at') FROM attempt AS a JOIN intent AS i
               ON i.id=json_extract(a.data,'$.intent_id')
               WHERE json_extract(a.data,'$.receipt.status')='sent'
                 AND json_extract(a.data,'$.receipt.platform_message_id')=?
                 AND json_extract(i.data,'$.conversation_id')=?
                 AND json_extract(i.data,'$.channel_id')=?
                 AND json_extract(i.data,'$.kind')!='react' LIMIT 1""",
            (ref.platform_message_id, ref.conversation_id, ref.channel_id),
        ).fetchone()
        return row[0] if row else None

    def mark(self, event):
        msg = event.message
        if msg is None:
            return event
        texts = [p.text.casefold() for p in msg.parts if p.type == "text"]

        def contains(name):
            return bool(name) and any(name.casefold() in t for t in texts)

        name = None
        if contains(self.names.get("internal")):
            name = "internal"
        elif any(contains(n) for n in self.names.get("external", {}).values()):
            name = "external"
        ref = msg.reply_to
        reply = False
        if (
            ref
            and ref.channel_id == event.channel_id
            and ref.conversation_id == event.conversation.conversation_id
        ):
            reply = self._sent_at(ref) is not None
        flags = Addressed(
            mention=any(p.type == "mention" and p.is_self for p in msg.parts),
            name=name,
            reply_to_self=reply,
        )
        return event.model_copy(update={"addressed": flags})

    def watermark(self):
        return self.ledger.db.execute("SELECT coalesce(max(rowid),0) FROM inbound").fetchone()[0]

    def focus_position(self, conversation, focus):
        if not focus or (focus.channel_id, focus.conversation_id) != (
            conversation.channel_id,
            conversation.conversation_id,
        ):
            return None
        row = self.ledger.db.execute(
            "SELECT rowid FROM inbound WHERE id=? AND json_extract(data,'$.conversation_id')=? "
            "AND coalesce(json_extract(data,'$.reason'),'')!='boundary_paused'",
            (f"{focus.channel_id}:{focus.platform_message_id}", focus.conversation_id),
        ).fetchone()
        if row:
            return row[0]
        # An outgoing focus has a receipt time but no inbound row of its own.
        at = self._sent_at(focus)
        if at is None:
            return None
        return self.ledger.db.execute(
            "SELECT coalesce(max(rowid),0) FROM inbound WHERE json_extract(data,'$.conversation_id')=? "
            "AND julianday(json_extract(data,'$.content_written_at'))<=julianday(?)",
            (focus.conversation_id, at),
        ).fetchone()[0]

    def messages_after(self, conversation, position):
        rows = self.ledger.db.execute(
            """SELECT data FROM inbound WHERE rowid>?
               AND json_extract(data,'$.conversation_id')=? AND json_extract(data,'$.channel_id')=?
               AND json_extract(data,'$.type')='conversation.message'
               AND coalesce(json_extract(data,'$.reason'),'')!='boundary_paused'
               ORDER BY rowid""",
            (position, conversation.conversation_id, conversation.channel_id),
        )
        events = []
        for (data,) in rows:
            value = json.loads(data)
            if value.get("message") and value["message"].get("parts") is not None:
                events.append(
                    EventEnvelope.model_validate(
                        {k: value[k] for k in EventEnvelope.model_fields if k in value}
                    )
                )
        return events
