"""Read only the test-conversation projection on the ledger owner's thread."""

import json


def records(ledger, conversation, since):
    db = ledger.db
    # Ledger timestamps are written in UTC. Keep full fractional precision.
    rows = db.execute(
        """SELECT r.id, json_extract(r.data,'$.kind'),
                  json_extract(r.data,'$.tool'), json_extract(r.data,'$.exec_status'),
                  json_extract(r.data,'$.delivery_status'), json_extract(r.data,'$.created_at'),
                  json_extract(e.data,'$.message.sender.actor_id')
             FROM record r LEFT JOIN inbound e ON e.id=json_extract(r.data,'$.source_event_id')
            WHERE json_extract(r.data,'$.conversation_id')=?
              AND (json_extract(r.data,'$.created_at')>=? OR r.id IN (
                    SELECT json_extract(i.data,'$.trace_id')
                      FROM attempt a JOIN intent i ON i.id=json_extract(a.data,'$.intent_id')
                     WHERE json_extract(a.data,'$.status')='sent'
                       AND json_extract(a.data,'$.receipt_written_at')>=?
              )) ORDER BY r.rowid""",
        (conversation, since, since),
    )
    items = {}
    for trace, kind, tool, execution, delivery, created, sender in rows:
        items[trace] = dict(
            trace_id=trace,
            kind=kind,
            tool=tool,
            exec_status=execution,
            delivery_status=delivery,
            created_at=created,
            deliveries=[],
            sender_id=sender,
            tokens={"input": 0, "output": 0},
            reaction_outcome=None,
        )
    if not items:
        return []
    selected = json.dumps(list(items))
    for trace, reaction, state, status, code in db.execute(
        """SELECT json_extract(i.data,'$.trace_id'), json_extract(i.data,'$.status_reaction'), json_extract(i.data,'$.reaction_state'),
                  json_extract(i.data,'$.status'), json_extract(i.data,'$.error.code')
             FROM intent i WHERE json_extract(i.data,'$.trace_id') IN (SELECT value FROM json_each(?))
            ORDER BY i.rowid""",
        (selected,),
    ):
        items[trace]["deliveries"].append(
            dict(
                state=state,
                status=status,
                error_code=code,
                status_reaction=bool(reaction),
            )
        )
    for trace, kind, inp, out, outcome in db.execute(
        """SELECT json_extract(a.data,'$.trace_id'), json_extract(a.data,'$.kind'), json_extract(a.data,'$.input_tokens'),
                  json_extract(a.data,'$.output_tokens'),json_extract(a.data,'$.reaction_outcome')
             FROM audit a WHERE json_extract(a.data,'$.trace_id') IN (SELECT value FROM json_each(?))
              AND json_extract(a.data,'$.kind') IN ('model.usage','task.reaction')""",
        (selected,),
    ):
        if kind == "model.usage":
            items[trace]["tokens"]["input"] += inp or 0
            items[trace]["tokens"]["output"] += out or 0
        elif outcome:
            items[trace]["reaction_outcome"] = outcome
    return list(items.values())
