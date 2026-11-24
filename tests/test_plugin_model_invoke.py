"""runtime-services-spec §6：插件文字调用只使用伴侣当前的模型。"""

import asyncio
import json
from datetime import timedelta

import pytest
import yaml

from clytie.agent.plugin_models import DAILY_USAGE_SQL
from clytie.backup.coordination import run_sync
from clytie.sdk import CancelToken
from clytie.sdk.testing import MockClock
from clytie.sdk.testing.samples import sample_call, sample_request
from clytie.sdk.types import ChatMessage, ImagePart, ModelTurn, ToolSpec
from clytie.supervisor.companion import Companion

from .host_helpers import SOURCE, enable, plugin
from .test_agent import finish, make_agent
from .test_agent_model_usage import usages
from .test_supervisor_startup import home_copy

PID = "fictional-reader"
PURPOSE = "news_judgment"


async def test_plugin_model_production_current_model_request_audit_and_daily_status(tmp_path):
    home = home_copy(tmp_path)
    source = """
from clytie.sdk import CancelToken
from clytie.sdk.testing.samples import sample_request


class Plugin:
    async def start(self, ctx):
        turn = await ctx.model.generate(sample_request(ctx.clock), CancelToken(), purpose="startup")
        self.startup_error = turn.error.code

    async def stop(self):
        pass
"""
    plugin(home, PID, source=source, permissions=["model.invoke"])
    cfg = home / "companions/companion_a/companion.yaml"
    options = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    options.setdefault("plugins", {})[PID] = {"enabled": True, "config": {}}
    cfg.write_text(yaml.safe_dump(options), encoding="utf-8")
    companion = Companion(home, "companion_a", MockClock())
    try:
        assert await companion.start()
        agent = companion.agent
        runtime = agent.host.runtimes[agent.cid, PID]
        assert runtime.plugin.startup_error == "model_unavailable"
        ctx = runtime.ctx
        model = agent.model_registration.implementation
        assert model.ctx.config == companion.report.plugin_configs[agent.cid]["model"]
        request = sample_request(agent.clock)
        seen = []

        async def generate(req, token):
            seen.append(req)
            return ModelTurn(
                text="fictional model result",
                finish="stop",
                usage={"input_tokens": 9, "cached_input_tokens": 2, "output_tokens": 3},
            )

        model.generate = generate
        result = await ctx.model.generate(request, CancelToken(), purpose=PURPOSE)
        assert result.text == "fictional model result"
        assert seen == [request]
        rows = await run_sync(agent.ledger, agent.ledger.rows, "audit")
        (row,) = [r for r in rows if r["kind"] == "model.usage"]
        assert row == {
            "kind": "model.usage",
            "trace_id": request.trace_id,
            "at": row["at"],
            "purpose": "plugin",
            "plugin": PID,
            "plugin_purpose": PURPOSE,
            "day": agent.clock.now().astimezone(agent.clock.local_tz).date().isoformat(),
            "duration_ms": 0,
            "input_tokens": 9,
            "cached_input_tokens": 2,
            "output_tokens": 3,
            "finish": "stop",
            "timing": "on_time",
        }
        for marker in (request.messages[0].parts[0].text, result.text):
            assert marker not in json.dumps(rows)
            assert marker not in repr(agent.host.log.entries)
        await companion.refresh_health()
        assert companion.health()["plugin_model_usage"] == {PID: {"count": 1, "tokens": 12}}
        assert await run_sync(agent.ledger, agent.ledger.rows, "record") == []
    finally:
        await companion.stop()


async def test_plugin_model_text_only_valid_purpose_and_protection(tmp_path):
    plugin(tmp_path, PID, permissions=["model.invoke"])
    agent, _, model = await make_agent(tmp_path)
    try:
        ctx = (await enable(agent.host, PID)).ctx
        req = sample_request(agent.clock)
        tool = ToolSpec(name="fictional", description="", when_not_to_use="", parameters={})
        for update in (
            {"tools": [tool]},
            {"messages": [ChatMessage(role="user", parts=[ImagePart(raw_ref="fictional")])]},
            {"messages": [ChatMessage(role="assistant", tool_calls=[sample_call()])]},
            {"messages": [ChatMessage(role="tool", tool_call_id="fictional")]},
        ):
            result = await ctx.model.generate(
                req.model_copy(update=update), CancelToken(), purpose=PURPOSE
            )
            assert result.error.code == "invalid_request"
        for purpose in ("fictional private content", "a" * 33):
            result = await ctx.model.generate(req, CancelToken(), purpose=purpose)
            assert result.error.code == "invalid_request"
            assert purpose not in repr(agent.host.log.entries)
        agent.protection.active = True
        assert (
            await ctx.model.generate(req, CancelToken(), purpose=PURPOSE)
        ).error.code == "quota_exhausted"
        assert model.requests == []
        assert usages(agent) == []
    finally:
        await finish(agent)


async def test_plugin_model_per_plugin_concurrency_and_rolling_minute(tmp_path):
    for pid in (PID, "other-reader"):
        plugin(tmp_path, pid, permissions=["model.invoke"])
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(req, cancel):
        started.set()
        await release.wait()
        return ModelTurn(finish="stop", usage={})

    agent, _, _ = await make_agent(tmp_path, delayed)
    try:
        first = (await enable(agent.host, PID)).ctx.model
        other = (await enable(agent.host, "other-reader")).ctx.model

        async def invoke(model):
            return await model.generate(sample_request(agent.clock), CancelToken(), purpose=PURPOSE)

        active = asyncio.create_task(invoke(first))
        await started.wait()
        assert (await invoke(first)).error.code == "rate_limited"
        assert (await invoke(other)).finish == "stop"
        release.set()
        assert (await active).finish == "stop"
        for _ in range(9):
            assert (await invoke(first)).finish == "stop"
        agent.clock.advance(59)
        assert (await invoke(first)).error.code == "rate_limited"
        assert (await invoke(other)).finish == "stop"
        agent.clock.advance(1)
        assert (await invoke(first)).finish == "stop"
        plan = agent.ledger.db.execute("EXPLAIN QUERY PLAN " + DAILY_USAGE_SQL, ("2000-01-01",))
        assert any("SEARCH audit USING INDEX plugin_usage_by_day" in row[3] for row in plan)
        assert agent.plugin_models.read_status() == {
            PID: {"count": 11, "tokens": 0},
            "other-reader": {"count": 2, "tokens": 0},
        }
        agent.clock.advance(24 * 3600)
        assert agent.plugin_models.read_status() == {}
    finally:
        release.set()
        await finish(agent)


@pytest.mark.parametrize("end", ["disable", "deadline", "caller_cancel", "short_deadline"])
async def test_plugin_model_lifetime_deadline_and_late_usage(tmp_path, end):
    plugin(tmp_path, PID, permissions=["model.invoke"])
    started, release = asyncio.Event(), asyncio.Event()
    tokens = []

    async def delayed(req, cancel):
        tokens.append(cancel)
        started.set()
        await release.wait()
        return ModelTurn(text="fictional late result", finish="stop", usage={"output_tokens": 8})

    agent, _, model = await make_agent(tmp_path, delayed)
    try:
        runtime = await enable(agent.host, PID)
        request = sample_request(agent.clock)
        seconds = 3 if end == "short_deadline" else 120
        deadline = agent.clock.now() + timedelta(seconds=seconds)
        request = request.model_copy(
            update={"budget": request.budget.model_copy(update={"deadline": deadline})}
        )
        token = CancelToken()
        active = asyncio.create_task(runtime.ctx.model.generate(request, token, purpose=PURPOSE))
        await started.wait()
        limit = min(60, seconds)
        assert model.requests[0].budget.deadline == agent.clock.now() + timedelta(seconds=limit)
        if end == "disable":
            await agent.host.apply(agent.host.plan_disable(agent.cid, PID).plan_id)
            with pytest.raises(asyncio.CancelledError):
                await active
            with pytest.raises(asyncio.CancelledError):
                await runtime.ctx.model.generate(request, CancelToken(), purpose=PURPOSE)
            new = (await enable(agent.host, PID)).ctx.model
            assert (
                await new.generate(request, CancelToken(), purpose=PURPOSE)
            ).error.code == "rate_limited"
        elif end == "caller_cancel":
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
        else:
            await agent.clock.advance_and_drain(limit)
            assert (await active).finish == "deadline"
        health = agent.model_health
        assert tokens[0].cancelled
        assert usages(agent) == []
        release.set()
        await asyncio.gather(*tuple(agent.kernel.delivery.background))
        assert agent.model_health == health
        (row,) = usages(agent)
        assert row["timing"] == "late" and row["plugin"] == PID and row["output_tokens"] == 8
        assert "fictional late result" not in json.dumps(agent.ledger.rows("audit"))
    finally:
        release.set()
        await finish(agent)


async def test_plugin_model_consolidator_only_gets_persona_during_consolidation(tmp_path):
    plugin(
        tmp_path,
        PID,
        source=SOURCE
        + """
    def create_consolidator(self):
        return self

    async def run(self, window, reader, cancel):
        await self.ask(cancel)
        return []

    async def service(self, method, args):
        from clytie.sdk import CancelToken
        await self.ask(CancelToken())
        return {}

    async def ask(self, cancel):
        await self.ctx.model.generate(self.request, cancel, purpose="memory.recollect")
""",
        permissions=["model.invoke"],
        contributes={
            "memory": {"consolidator": {"factory": "create_consolidator"}},
            "services": [
                {
                    "name": "example.summary",
                    "version": "1.0",
                    "methods": ["get"],
                    "handler": "service",
                }
            ],
        },
    )
    agent, _, model = await make_agent(
        tmp_path,
        changes={
            "memory": {"recollect": {"consolidator": PID}},
            "plugins": {PID: {"enabled": True}},
        },
    )
    try:
        runtime = agent.host.runtimes[agent.cid, PID]
        runtime.plugin.request = sample_request(agent.clock)
        (reg,) = agent.host.registrations(agent.cid, "memory.consolidator")
        await agent.host.invoke(
            agent.cid,
            PID,
            reg.point,
            reg.key,
            None,
            None,
            CancelToken(),
            method="run",
            scene="owner_private",
        )
        await agent.host.invoke(agent.cid, PID, "service", "example.summary", "get", {})
        assert model.requests[0].messages[0].parts[0].text == agent.persona.render("owner_private")
        assert model.requests[1] == runtime.plugin.request
        assert [
            r["kind"]
            for r in agent.ledger.rows("audit")
            if r["kind"] in {"memory.recollect_model", "model.usage"}
        ] == ["memory.recollect_model", "model.usage"]
        assert usages(agent)[0]["plugin"] == PID
    finally:
        await finish(agent)
