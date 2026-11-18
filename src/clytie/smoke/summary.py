"""Read only the test-conversation projection on the ledger owner's thread."""

import json


def records(ledger, conversation, since):
    db = ledger.db
    # Ledger timestamps are written in UTC. Keep full fractional precision.
    rows = db.execute(
        """SELECT json_extract(data,'$.trace_id'), json_extract(data,'$.kind'),
                  json_extract(data,'$.tool'), json_extract(data,'$.exec_status'),
                  json_extract(data,'$.delivery_status'), json_extract(data,'$.created_at'),
                  json_extract(data,'$.source_event_id')
             FROM record WHERE json_extract(data,'$.conversation_id')=?
               AND json_extract(data,'$.created_at')>=? ORDER BY rowid""",
        (conversation, since),
    )
    items = {}
    for trace, kind, tool, execution, delivery, created, event in rows:
        inbound = db.execute(
            "SELECT json_extract(data,'$.message.ref.platform_message_id') FROM inbound WHERE id=?",
            (event,),
        ).fetchone()
        items[trace] = dict(
            trace_id=trace,
            kind=kind,
            tool=tool,
            exec_status=execution,
            delivery_status=delivery,
            created_at=created,
            deliveries=[],
            platform_message_id=inbound[0] if inbound else None,
            tokens={"input": 0, "output": 0},
            reaction_outcome=None,
        )
    if not items:
        return []
    # Join on the stored record ID so an old 'since' never expands the SQL parameter list.
    for trace, intent, state, status, code, attempts in db.execute(
        """SELECT r.id, i.id, json_extract(i.data,'$.reaction_state'),
                  json_extract(i.data,'$.status'), json_extract(i.data,'$.error.code'),
                  json_extract(i.data,'$.attempts')
             FROM intent i JOIN record r ON r.id=json_extract(i.data,'$.trace_id')
            WHERE json_extract(r.data,'$.conversation_id')=? AND json_extract(r.data,'$.created_at')>=?
            ORDER BY i.rowid""",
        (conversation, since),
    ):
        receipt = db.execute(
            "SELECT json_extract(data,'$.receipt.platform_message_id') FROM attempt WHERE id=?",
            (json.dumps([intent, attempts], separators=(",", ":")),),
        ).fetchone()
        items[trace]["deliveries"].append(
            dict(
                state=state,
                status=status,
                error_code=code,
                platform_message_id=receipt[0] if receipt else None,
            )
        )
    for trace, kind, inp, out, outcome in db.execute(
        """SELECT r.id, json_extract(a.data,'$.kind'), json_extract(a.data,'$.input_tokens'),
                  json_extract(a.data,'$.output_tokens'),json_extract(a.data,'$.reaction_outcome')
             FROM audit a JOIN record r ON r.id=json_extract(a.data,'$.trace_id')
            WHERE json_extract(r.data,'$.conversation_id')=? AND json_extract(r.data,'$.created_at')>=?
              AND json_extract(a.data,'$.kind') IN ('model.usage','task.reaction')""",
        (conversation, since),
    ):
        if kind == "model.usage":
            items[trace]["tokens"]["input"] += inp or 0
            items[trace]["tokens"]["output"] += out or 0
        elif outcome:
            items[trace]["reaction_outcome"] = outcome
    return list(items.values())
