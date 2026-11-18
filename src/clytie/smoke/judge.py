"""Judge sanitized platform observations against one matching ledger record."""


def verdict(case, record, replies, events, now, interval, started, ledger_terminal_at=None):
    if record is None:
        return "timeout_record"
    ds = record["deliveries"]
    statuses = [d["status"] for d in ds]

    def fail(reason):
        return {
            "result": "failed",
            "failure": reason,
            "receipts": statuses,
            "tokens": record["tokens"],
        }

    if (
        record["exec_status"] in {"error", "interrupted", "cancelled", "denied"}
        or record["reaction_outcome"] == "tool_failed"
    ):
        return fail("execution_failed")
    if any(s in {"failed", "unknown"} for s in statuses):
        return fail("receipt_failed")
    if (
        record["exec_status"] != "ok"
        or record["delivery_status"] == "pending"
        or any(s != "sent" for s in statuses)
    ):
        return "timeout_execution"
    expected_count = sum(not delivery["status_reaction"] for delivery in ds)
    if len(replies) > expected_count:
        return fail("unattributed_reply")
    if len(replies) < expected_count:
        return "timeout_replies"
    expect = case["expect"]
    if bool(replies) != expect["reply"] or any(r["image"] for r in replies) != expect["image"]:
        return fail("reply_mismatch")
    remaining = set()
    adds = []
    for state, add, at in events:
        if add:
            adds.append((state, at))
            remaining.add(state)
        else:
            remaining.discard(state)
    ledger_states = [d["state"] for d in ds if d["state"]]
    source = "platform" if adds else "ledger"
    actual = [s for s, _ in adds] or ledger_states
    wanted = expect["reactions"]
    last_reply = max((r["at"] for r in replies), default=started)
    if wanted == "none":
        if actual or events:
            return fail("unexpected_reaction")
        if now < last_reply + interval + 2:
            return "timeout_reactions"
        final = "verified"
    else:
        terminal = wanted[-1]
        if terminal not in actual and terminal not in ledger_states:
            return "timeout_reactions"
        if (
            now
            < max(
                last_reply,
                max(
                    (at for s, at in adds if s == terminal),
                    default=ledger_terminal_at or last_reply,
                ),
            )
            + interval
            + 3
        ):
            return "timeout_reactions"
        # A new add after the terminal add is positive evidence, unlike a
        # missing removal notification. Allow late removals during the window.
        last_terminal = max((at for state, at in adds if state == terminal), default=None)
        if last_terminal is not None and (
            terminal not in remaining
            or any(s != terminal and s in remaining and at > last_terminal for s, at in adds)
        ):
            return {**fail("final_reaction_failed"), "final_status": "failed"}
        index = 0
        for entry in wanted:
            if index < len(actual) and actual[index] == entry.removeprefix("optional:"):
                index += 1
            elif not entry.startswith("optional:"):
                return fail("reaction_order")
        if index != len(actual):
            return fail("reaction_order")
        final = "verified" if adds and remaining == {terminal} else "unverified"
    return {
        "result": "needs_visual_check" if final == "unverified" else "passed",
        "final_status": final,
        "final_reaction": actual[-1] if actual else None,
        "reaction_source": source,
        "reactions": [[s, round(at - started, 3)] for s, at in adds]
        or [[s, None] for s in ledger_states],
        "receipts": statuses,
        "tokens": record["tokens"],
        "replies": [
            {
                "chars": r["chars"],
                "text": r["chars"] > 0,
                "image": r["image"],
                "at_s": round(r["at"] - started, 3),
            }
            for r in replies
        ],
        "reply_s": round(last_reply - started, 3),
        "failure": None,
    }
