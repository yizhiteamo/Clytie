"""主动任务的生成、改写、分段与交付经过真实插件宿主和账本。"""

import asyncio

import pytest

from clytie.agent.policy import text_parts
from clytie.clock import SystemClock
from clytie.plugin_host.support import timed

from .host_helpers import SOURCE, enable, plugin
from .test_agent import finish, make_agent
from .test_agent_acceptance import ECHO


@pytest.mark.parametrize(("group_seconds", "delivered"), [(60, 6), (10, 4)])
async def test_proactive_tool_reply_preserves_kind_through_rewrite_and_delivery(
    tmp_path, monkeypatch, group_seconds, delivered
):
    plugin(
        tmp_path,
        "draft-kind-probe",
        SOURCE
        + """
    async def observe(self, draft):
        self.seen.append((draft.kind, draft.task_id))
        return draft.model_copy(update={"kind": "reply"})
""",
        contributes={"behavior": {"after_generation": {"handler": "observe"}}},
        priority=10,
    )
    paragraphs = [f"虚构的第{i}段主动任务回复。" for i in range(8)]
    agent, channel, model = await make_agent(
        tmp_path,
        ECHO,
        {"finish": "stop", "text": "fictional rewrite marker"},
        {"finish": "stop", "text": "\n\n".join(paragraphs)},
        changes={
            "proactive": {"tools": {"echo": {"daily": 1}}},
            "delivery": {"group_max_seconds": group_seconds},
            "outbound": {"style_words": ["fictional rewrite marker"]},
        },
    )
    first = asyncio.Event()
    sent_at = []
    deliver = channel.send

    async def observe_send(intent, cancel):
        sent_at.append(agent.clock.now())
        receipt = await deliver(intent, cancel)
        first.set()
        return receipt

    monkeypatch.setattr(channel, "send", observe_send)
    try:
        agent.host.configure(
            agent.cid,
            plugins={"reply-form": {"config": {"tuning": {"interval_seconds_proactive": [3, 3]}}}},
        )
        probe = await enable(agent.host, "draft-kind-probe")
        await enable(agent.host, "reply-form")
        submission = await agent.submit(
            "owner_private", topic="虚构话题", reason="festival", tools=True
        )
        await timed(first.wait(), SystemClock(), 5)
        await agent.clock.advance_and_drain(20)
        await agent.drain()

        assert submission.status == "accepted"
        record = agent.ledger.get("record", submission.trace_id)
        task_id = record["task_id"]
        assert task_id and record["tool_rounds"] == 1 and len(model.requests) == 3
        assert probe.plugin.seen == [("proactive", task_id), ("proactive", task_id)]
        assert (
            sum(
                name == "plugin.invalid_form_change"
                and fields.get("plugin_id") == "draft-kind-probe"
                for _, name, fields in agent.host.log.entries
            )
            == 2
        )
        expected = [*paragraphs[:5], "\n\n".join(paragraphs[5:])]
        assert [text_parts(i.parts) for i in channel.sent] == expected[:delivered]
        assert [i.split.index for i in channel.sent] == list(range(delivered))
        assert all(
            i.split.total == 6 and i.kind == "proactive" and i.task_id == task_id
            for i in channel.sent
        )
        assert [(b - a).total_seconds() for a, b in zip(sent_at, sent_at[1:])] == [3] * (
            delivered - 1
        )
        failed = [i for i in agent.ledger.rows("intent") if i["status"] == "failed"]
        assert [i["error"]["code"] for i in failed] == ["group_timeout"] * (6 - delivered)
    finally:
        await agent.clock.advance_and_drain(60)
        await finish(agent)
