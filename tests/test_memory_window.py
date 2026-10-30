"""乱序收据不能改变窗口前缀；长消息不拆改原文。"""

from datetime import timedelta

from clytie.memory.window import fresh_rows, latest_activity

from .test_agent import finish, make_agent


async def test_reordered_receipts_do_not_reorder_intent_prefix(tmp_path):
    agent, _, _ = await make_agent(tmp_path)
    try:
        ledger = agent.ledger
        at = agent.clock.now()
        with ledger.transaction():
            for index in range(2):
                key = f"intent-{index}"
                ledger.put(
                    "intent",
                    key,
                    {
                        "intent_id": key,
                        "conversation_id": "test_group",
                        "created_at": (at + timedelta(seconds=index)).isoformat(),
                        "status": "sent",
                        "parts": [{"type": "text", "text": "虚构消息"}],
                    },
                )
                ledger.put(
                    "attempt",
                    key,
                    {
                        "intent_id": key,
                        "started_at": at.isoformat(),
                        "receipt_written_at": (at + timedelta(seconds=20 - index)).isoformat(),
                    },
                )
        rows, end = fresh_rows(ledger, {"inbound": 0, "intent": 0}, "test_group")
        assert [r[2] for r in rows] == [1, 2]
        assert latest_activity(ledger, {"inbound": 0, "intent": 0}, "test_group") == at + timedelta(
            seconds=20
        )
        assert end["intent"] == 2
    finally:
        await finish(agent)
