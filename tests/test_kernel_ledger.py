"""账本封口、原子防重、不可见骨架和重启不重放。"""

from uuid import uuid4

import pytest

from clytie.kernel.ledger import Ledger
from clytie.kernel.projection import redact
from clytie.sdk.types import DeliveryIntent, Receipt, ToolCall

from .test_kernel_reactions import fixture


def intent(ledger, trace, **kwargs):
    r = ledger.get("record", trace)
    return DeliveryIntent(
        intent_id=uuid4().hex,
        companion_id=r["companion_id"],
        conversation_id=r["conversation_id"],
        channel_id=r["channel_id"],
        kind="reply",
        trace_id=trace,
        task_id=r["task_id"],
        parts=[],
        **kwargs,
    )


def test_restart_never_replays_and_unknown_never_retries(tmp_path):
    ledger, _, _, clock, trace = fixture(tmp_path)
    first, pending = intent(ledger, trace), intent(ledger, trace)
    ledger.register_intents(trace, [first, pending])
    ledger.begin_attempt(first.intent_id)
    path = ledger.path
    ledger.close()
    restarted = Ledger(path, "companion_a", clock)
    assert restarted.restart() == [trace]
    assert restarted.get("record", trace)["exec_status"] == "interrupted"
    assert restarted.get("intent", first.intent_id)["status"] == "unknown"
    assert restarted.get("intent", first.intent_id)["error"]["code"] == "receipt_unknown_on_restart"
    assert restarted.get("intent", pending.intent_id)["error"]["code"] == "not_sent_before_restart"
    with pytest.raises(ValueError):
        restarted.begin_attempt(first.intent_id)
    assert restarted.restart() == []
    restarted.close()


def test_duplicate_inbound_and_invocation_survive_reopen(tmp_path):
    ledger, _, _, clock, trace = fixture(tmp_path)
    from clytie.sdk.types import EventEnvelope

    e = ledger.get("inbound", "fake:example")
    event = EventEnvelope.model_validate(
        {k: v for k, v in e.items() if k in EventEnvelope.model_fields}
    )
    assert ledger.claim_inbound(event) == ("fake:example", False)
    row, first = ledger.claim_invocation(
        trace,
        "echo-tool",
        ToolCall(call_id="again", tool="echo", arguments={}),
        "example",
        timeout=600,
        schema={},
    )
    assert first is False and len(ledger.rows("invocation")) == 1
    assert ledger.get("record", trace)["task_id"]
    ledger.close()


def test_atomic_seal_rolls_back_invalid_group_and_forbids_late_intents(tmp_path):
    ledger, _, _, _, trace = fixture(tmp_path)
    good = intent(ledger, trace)
    wrong = good.model_copy(update={"intent_id": uuid4().hex, "companion_id": "companion_b"})
    with pytest.raises(ValueError):
        ledger.seal(trace, "ok", [good, wrong])
    assert ledger.intents(trace) == []
    assert ledger.get("record", trace)["exec_status"] == "running"
    assert ledger.seal(trace, "ok", [good])
    assert not ledger.seal(trace, "error")
    with pytest.raises(ValueError):
        ledger.register_intents(trace, [intent(ledger, trace)])
    ledger.close()


def test_unknown_late_receipt_corrects_only_matching_attempt(tmp_path):
    ledger, _, _, clock, trace = fixture(tmp_path)
    i = intent(ledger, trace)
    ledger.seal(trace, "ok", [i])
    n = ledger.begin_attempt(i.intent_id)
    ledger.record_receipt(
        i.intent_id, n, Receipt(intent_id=i.intent_id, status="unknown", at=clock.now())
    )
    assert not ledger.record_receipt(
        i.intent_id, n, Receipt(intent_id=i.intent_id, status="sent", at=clock.now())
    )
    assert ledger.get("record", trace)["delivery_status"] == "sent"
    assert not ledger.record_receipt(
        i.intent_id, n, Receipt(intent_id=i.intent_id, status="failed", at=clock.now())
    )
    assert ledger.get("intent", i.intent_id)["status"] == "sent"
    assert {a["kind"] for a in ledger.rows("audit")} >= {"late_receipt", "conflicting_receipt"}
    ledger.close()


def test_projection_hides_even_skeleton_and_sensitive_union_redaction(tmp_path):
    ledger, _, _, _, trace = fixture(tmp_path)
    assert ledger.project(scene="public_group", conversation_id="other_group") == []
    assert ledger.project(scene="stranger_private", conversation_id="other_private") == []
    assert len(ledger.project(scene="owner_private", conversation_id="owner")) == 1
    with ledger.transaction():
        r = ledger.get("record", trace)
        r.update(scene="owner_private", conversation_id="owner")
        ledger.put("record", trace, r)
    assert ledger.project(scene="family_group", conversation_id="family") == []
    assert (
        len(
            ledger.project(
                scene="family_group", conversation_id="family", private_scenes=["family_group"]
            )
        )
        == 1
    )
    schema = {
        "anyOf": [
            {
                "properties": {
                    "items": {"items": {"properties": {"credential": {"x-clytie-sensitive": True}}}}
                }
            },
            {"properties": {"other": {"x-clytie-sensitive": True}}},
        ]
    }
    data = {"items": [{"credential": "fictional", "keep": "example"}], "other": "fictional"}
    assert redact(data, schema) == {
        "items": [{"credential": "[redacted]", "keep": "example"}],
        "other": "[redacted]",
    }
    assert data["other"] == "fictional"
    ledger.close()
