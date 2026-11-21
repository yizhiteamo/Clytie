"""B7a §3. Judge the actual fake-model request, including late or malformed turns."""

import asyncio
import json
from datetime import timedelta

import pytest

from clytie.agent.judgment import AttentionJudgments
from clytie.sdk import CancelToken
from clytie.sdk.behavior import JudgeFacts
from clytie.sdk.types import ModelTurn

from .test_agent import event, finish, make_agent

FACTS = JudgeFacts(
    participants=3,
    share=0.25,
    rounds_with_same_person=1,
    counterpart_fast=False,
    fatigue="some",
    ignored_recently=False,
    owner_spoke=False,
    name_hit=False,
)
ANSWER = {"join": True, "focus": None, "new_topic": False}


def group(agent, text, **options):
    return event(agent, text, scene="public_group", role="member", **options)


def history(agent, item, state="ignored", reason=None):
    ledger = agent.ledger
    ledger.claim_inbound(item)
    with ledger.transaction():
        row = ledger.get("inbound", item.event_id)
        ledger.put("inbound", item.event_id, dict(row, state=state, reason=reason))


def text(msg):
    assert all(part.type == "text" for part in msg.parts)
    return "\n".join(part.text for part in msg.parts)


async def test_judge_request_projection_media_persona_focus_and_usage(tmp_path):
    async def response(req, cancel):
        return ModelTurn(
            text=json.dumps(dict(ANSWER, focus=1, calling_me=True)),
            finish="stop",
            usage={"input_tokens": 12, "output_tokens": 3},
        )

    agent, _, model = await make_agent(
        tmp_path,
        response,
        changes={"attention": {"judge": {"persona_chars": 9, "messages": 2}}},
    )
    try:
        history(agent, group(agent, "older discarded"))
        visible = group(agent, "recent skipped chat")
        history(agent, visible)
        history(agent, group(agent, "recent merged chat"), "merged")
        history(agent, group(agent, "boundary secret"), reason="boundary_paused")
        history(agent, group(agent, "restart secret"), state="dropped_on_restart")
        private = event(agent, "fictional-private-source")
        history(agent, private)
        current = group(agent, "current fictional_internal", reply_to=private.message.ref)
        current = current.model_copy(
            update={
                "message": current.message.model_copy(
                    update={
                        "parts": [
                            *current.message.parts,
                            {"type": "image", "raw_ref": "fictional-image-key"},
                            {
                                "type": "sticker",
                                "raw_ref": "fictional-sticker-key",
                                "sticker_id": "fake",
                            },
                        ]
                    }
                )
            }
        )
        judge = AttentionJudgments(agent)
        result = await judge.decide(
            current.conversation, [current], FACTS.model_copy(update={"name_hit": True})
        )
        assert result.focus == visible.message.ref
        assert result.status == "ok" and result.join and result.calling_me
        (req,) = model.requests
        assert not req.tools and req.budget.max_tool_rounds == 0
        texts = [text(msg) for msg in req.messages]
        assert texts[1] == "Fictional"
        assert [part.split("：")[-1] for part in texts[2:4]] == [
            "recent skipped chat",
            "recent merged chat",
        ]
        assert "看不到的消息" in texts[4] and "current fictional_internal" in texts[4]
        assert "[图片2]" in texts[4] and "[表情3]" in texts[4]
        assert all(
            "fictional_internal" not in text(msg) for msg in req.messages if msg.role == "system"
        )
        joined = "\n".join(texts)
        for forbidden in (
            "older discarded",
            "fictional private detail",
            "boundary secret",
            "fictional-private-source",
            "restart secret",
            "fictional-image-key",
            "fictional-sticker-key",
            "能力状态",
            "core_recall",
            "最近任务",
        ):
            assert forbidden not in joined
        assert req.budget.deadline == agent.clock.now() + timedelta(seconds=20)
        rows = [r for r in agent.ledger.rows("audit") if r.get("purpose") == "attention"]
        (row,) = rows
        assert (row["kind"], row["status"], row["focus"], row["join"], row["calling_me"]) == (
            "model.usage",
            "ok",
            1,
            True,
            True,
        )
        assert (row["input_tokens"], row["output_tokens"], row["timing"]) == (12, 3, "on_time")
        audit = json.dumps(row, ensure_ascii=False)
        assert "fictional_internal" not in audit and "recent skipped chat" not in audit
        assert visible.message.ref.platform_message_id not in audit
        assert await judge.status() == {"test_group": {"count": 1, "tokens": 15}}
        agent.clock.advance(24 * 3600)
        assert await judge.status() == {}
    finally:
        await finish(agent)


async def test_judge_budget_keeps_recent_history_and_complete_batch(tmp_path):
    agent, _, model = await make_agent(tmp_path, {"text": json.dumps(ANSWER), "finish": "stop"})
    try:
        history(agent, group(agent, "discarded"))
        history(agent, group(agent, "长" * 4000))
        a, b = group(agent, "A" * 3100), group(agent, "batch B")
        await AttentionJudgments(agent).decide(a.conversation, [a, b], FACTS)
        chats = [text(m) for m in model.requests[0].messages if m.role == "user"]
        assert len(chats) == 3 and len(chats[0]) <= 3000
        assert "长" in chats[0] and "discarded" not in chats[0]
        assert a.message.parts[0].text in chats[1] and "batch B" in chats[2]
    finally:
        await finish(agent)


async def test_scene_persona_and_visible_quote_without_duplicate_history(tmp_path):
    agent, _, model = await make_agent(tmp_path, {"text": json.dumps(ANSWER), "finish": "stop"})
    try:
        visible = group(agent, "fictional public quote")
        history(agent, visible)
        msg = group(agent, "current", reply_to=visible.message.ref)

        async def forbidden_memory(*args):
            pytest.fail("judgment must not retrieve memories")

        agent.memory_context = forbidden_memory
        await AttentionJudgments(agent).decide(msg.conversation, [msg], FACTS)
        (request,) = model.requests
        assert text(request.messages[1]) == "Fictional persona \n"
        chats = [text(m) for m in request.messages if m.role == "user"]
        assert len(chats) == 2 and "fictional public quote" in chats[0]
        assert "本批消息" in chats[1] and "引用：fictional public quote" in chats[1]
    finally:
        await finish(agent)


@pytest.mark.parametrize(
    "turn",
    [
        {"finish": "error"},
        {"finish": "stop", "text": "fictional-model-garbage"},
        {"finish": "stop", "text": json.dumps(dict(ANSWER, join="yes"))},
        {"finish": "stop", "text": json.dumps(dict(ANSWER, focus=99))},
        {"finish": "stop", "text": json.dumps(dict(ANSWER, injected="fictional-model-garbage"))},
    ],
)
async def test_judge_invalid_turn_fails_once(tmp_path, turn):
    agent, _, model = await make_agent(tmp_path, turn)
    try:
        msg = group(agent, "fictional input")
        judge = AttentionJudgments(agent)
        result = await judge.decide(msg.conversation, [msg], FACTS)
        assert result.status == "failed" and len(model.requests) == 1
        (row,) = [r for r in agent.ledger.rows("audit") if r.get("purpose") == "attention"]
        assert row["status"] == "failed" and row["input_tokens"] is None
        assert "fictional-model-garbage" not in json.dumps(row)
    finally:
        await finish(agent)


@pytest.mark.parametrize("cancelled", [True, False])
async def test_judge_deadline_or_cancel_counts_late_tokens_once_and_discards_text(
    tmp_path, cancelled
):
    started, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def late(req, token):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            observed.append(token.cancelled)
            await release.wait()
        return ModelTurn(text="late fictional secret", finish="stop", usage={"input_tokens": 7})

    agent, _, model = await make_agent(tmp_path, late)
    try:
        msg, token = group(agent, "fictional-input-secret"), CancelToken()
        judge = AttentionJudgments(agent)
        call = asyncio.create_task(judge.decide(msg.conversation, [msg], FACTS, token=token))
        await started.wait()
        if cancelled:
            token.cancel("test")
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            await agent.clock.advance_and_drain(20)
            assert (await call).status == "failed"
        assert await judge.status() == {"test_group": {"count": 1, "tokens": 0}}
        await agent.clock.advance_and_drain(agent.kernel.grace)
        release.set()
        await asyncio.gather(*tuple(agent.kernel.delivery.background))
        assert observed == [True] and len(model.requests) == 1
        audit = agent.ledger.rows("audit")
        rows = [r for r in audit if r.get("purpose") == "attention" and r["kind"] == "model.usage"]
        (row,) = rows
        assert (row["status"], row["timing"], row["input_tokens"]) == ("failed", "late", 7)
        assert "late fictional secret" not in json.dumps(audit)
        assert "fictional-input-secret" not in json.dumps(audit)
        assert await judge.status() == {"test_group": {"count": 1, "tokens": 7}}
    finally:
        release.set()
        await finish(agent)


async def test_recheck_only_new_visible_messages_and_draft_fails_open_on_exception(tmp_path):
    async def error(req, token):
        raise ValueError("fake exception content")

    agent, _, model = await make_agent(
        tmp_path, {"finish": "stop", "text": '{"send":false}'}, error
    )
    try:
        history(agent, group(agent, "older group history"))
        msg = group(agent, "new group message")
        elsewhere = event(agent, "private secret")
        judge = AttentionJudgments(agent)
        for expected in (False, True):
            assert (
                await judge.recheck(msg.conversation, [elsewhere, msg], "fictional draft")
                is expected
            )
        assert len(model.requests) == 2
        for req in model.requests:
            body = "\n".join(text(m) for m in req.messages)
            assert not req.tools and "fictional draft" in body and "new group message" in body
            assert "private secret" not in body and "older group history" not in body
        rows = [r for r in agent.ledger.rows("audit") if r.get("purpose") == "attention"]
        assert [r["kind"] for r in rows] == ["model.usage", "attention.failed"]
        assert rows[0]["send"] is False
        assert not any(
            s in json.dumps(rows) for s in ("fictional draft", "new group", "exception content")
        )
        assert await judge.status() == {"test_group": {"count": 2, "tokens": 0}}
    finally:
        await finish(agent)
