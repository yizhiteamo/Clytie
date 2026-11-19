"""agent-spec §11.1: usage is per returned turn, never inferred billing."""

import asyncio
import json

import pytest

from clytie.kernel.delivery import OwnedFinalization
from clytie.memory.scheduler import RecollectionModel
from clytie.sdk import CancelToken
from clytie.sdk.testing.samples import sample_request
from clytie.sdk.types import ModelTurn

from .test_agent import event, finish, make_agent
from .test_agent_acceptance import ECHO


def usages(agent):
    return [r for r in agent.ledger.rows("audit") if r["kind"] == "model.usage"]


@pytest.mark.parametrize("ending", ["stop", "error"])
async def test_usage_single_result_and_safe_fields(tmp_path, ending):
    async def response(req, cancel):
        return ModelTurn(
            text="fictional_reply_marker",
            finish=ending,
            usage={"input_tokens": 12, "output_tokens": 7},
        )

    agent, _, _ = await make_agent(tmp_path, response)
    try:
        trace = await agent.receive(event(agent, "fictional_credential_marker"))
        await agent.drain()
        rows = usages(agent)
        assert len(rows) == 1
        row = rows[0]
        assert (
            row.items()
            >= dict(
                trace_id=trace,
                purpose="reply",
                plugin="fake-model",
                input_tokens=12,
                output_tokens=7,
                image_count=0,
                image_bytes=0,
                finish=ending,
                timing="on_time",
            ).items()
        )
        assert set(row) == {
            "kind",
            "at",
            "trace_id",
            "purpose",
            "plugin",
            "input_tokens",
            "output_tokens",
            "image_count",
            "image_bytes",
            "finish",
            "timing",
        }
        assert "fictional_credential_marker" not in json.dumps(rows)
        assert "fictional_reply_marker" not in json.dumps(rows)
    finally:
        await finish(agent)


async def test_usage_tool_rounds_unknown_and_recollection_not_duplicated(tmp_path):
    agent, _, _ = await make_agent(tmp_path, ECHO, {"finish": "stop", "text": "done"})
    try:
        await agent.receive(event(agent))
        await agent.drain()
        assert len(usages(agent)) == 2
        assert all(r["input_tokens"] is None and r["output_tokens"] is None for r in usages(agent))
        await RecollectionModel(agent).generate(
            sample_request(agent.clock), CancelToken(), purpose="memory.recollect"
        )
        assert len(usages(agent)) == 2
        assert (
            len([r for r in agent.ledger.rows("audit") if r["kind"] == "memory.recollect_model"])
            == 1
        )
    finally:
        await finish(agent)


async def direct(agent, token, *, rewrite=False):
    trace = "a" * 32
    with agent.ledger.transaction():
        agent.ledger.put("record", trace, {"deadline": None})
    finalization = OwnedFinalization(agent.kernel.delivery)
    try:
        return await agent._generate(
            trace, [], [], event(agent).conversation, token, finalization, rewrite=rewrite
        )
    finally:
        finalization.release()


async def test_usage_rewrite(tmp_path):
    agent, _, _ = await make_agent(tmp_path)
    try:
        await direct(agent, CancelToken(), rewrite=True)
        assert [r["purpose"] for r in usages(agent)] == ["rewrite"]
    finally:
        await finish(agent)


async def test_usage_exception_has_no_invented_result(tmp_path):
    async def response(req, cancel):
        raise ValueError("fictional_failure")

    agent, _, _ = await make_agent(tmp_path, response)
    try:
        with pytest.raises(ValueError):
            await direct(agent, CancelToken())
        await agent.drain()
        assert usages(agent) == []
        assert agent.ledger.get("record", "a" * 32)["model_calls"] == 1
    finally:
        await finish(agent)


async def test_usage_cancel_after_observation_stays_on_time_once(tmp_path):
    agent, _, _ = await make_agent(tmp_path)
    token = CancelToken()
    audit = agent.ledger.audit

    def cancel_during_audit(kind, **data):
        audit(kind, **data)
        if kind == "model.usage":
            token.cancel("test")

    agent.ledger.audit = cancel_during_audit
    try:
        with pytest.raises(asyncio.CancelledError):
            await direct(agent, token)
        await agent.drain()
        assert [r["timing"] for r in usages(agent)] == ["on_time"]
    finally:
        await finish(agent)


async def test_usage_cancel_late_once(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def response(req, cancel):
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return ModelTurn(text="fictional_late", finish="stop", usage={"input_tokens": 9})

    agent, _, _ = await make_agent(tmp_path, response)
    token = CancelToken()
    call = asyncio.create_task(direct(agent, token))
    try:
        await started.wait()
        token.cancel("test")
        await agent.clock.advance_and_drain(agent.kernel.grace)
        with pytest.raises(asyncio.CancelledError):
            await call
        assert usages(agent) == []
        release.set()
        await asyncio.gather(*tuple(agent.kernel.delivery.background))
        assert [r["timing"] for r in usages(agent)] == ["late"]
        assert len([r for r in agent.ledger.rows("audit") if r["kind"] == "model.late_result"]) == 1
    finally:
        release.set()
        await finish(agent)


async def test_usage_cancel_already_received_before_result_is_late(tmp_path):
    token = CancelToken()

    async def response(req, cancel):
        token.cancel("test")
        return ModelTurn(finish="stop", usage={"output_tokens": 3})

    agent, _, _ = await make_agent(tmp_path, response)
    try:
        with pytest.raises(asyncio.CancelledError):
            await direct(agent, token)
        await asyncio.gather(*tuple(agent.kernel.delivery.background))
        assert [r["timing"] for r in usages(agent)] == ["late"]
    finally:
        await finish(agent)
