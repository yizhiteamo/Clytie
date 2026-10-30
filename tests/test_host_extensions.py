"""测试里新定义的插件不改宿主即可装配及调用全部扩展点。"""

import json
from types import SimpleNamespace

import pytest

from clytie.plugin_host import PluginFailure
from clytie.sdk import CancelToken
from clytie.sdk.testing.context_fakes import FakeApiClient, FakeCandidateReader, FakeIdentity
from clytie.sdk.testing.samples import (
    sample_actor,
    sample_call,
    sample_conversation,
    sample_event,
    sample_intent,
    sample_invocation,
    sample_memory,
    sample_query,
    sample_request,
    sample_task_summary,
)
from clytie.sdk.types import ContextRequest, Notice, ReplyDraft, Tick

from .host_helpers import SOURCE, enable, host, plugin

SOURCE_ALL = (
    SOURCE
    + """
    def create_model(self):
        from clytie.sdk.testing import FakeModel
        return FakeModel(self.ctx)
    def create_channel(self):
        from clytie.sdk.testing import FakeChannel
        from clytie.sdk.testing.samples import sample_event
        return FakeChannel(self.ctx, incoming=(sample_event(self.ctx.clock),))
    def create_embedder(self):
        from clytie.sdk.testing import FakeEmbedder
        return FakeEmbedder(self.ctx)
    def create_tool(self):
        from clytie.sdk.testing import FakeCapability
        return FakeCapability(self.ctx)
    def create_retriever(self):
        from clytie.sdk.testing import FakeRetriever
        return FakeRetriever(self.ctx)
    def create_importer(self):
        from clytie.sdk.testing import FakeImporter
        from clytie.sdk.testing.samples import sample_import_candidate
        return FakeImporter((sample_import_candidate(self.ctx.clock),), self.ctx)
    def create_consolidator(self):
        from clytie.sdk.testing import FakeConsolidator
        from clytie.sdk.types import RecollectItem
        return FakeConsolidator((RecollectItem(kind="episode", content="fictional", evidence=[1]),), self.ctx)
    def create_notifier(self):
        from clytie.sdk.testing import FakeNotifier
        return FakeNotifier(self.ctx)
    async def observe(self, value):
        self.seen.append(value)
    async def before_attention(self, value):
        from clytie.sdk.behavior import Normal
        return Normal()
    async def attention(self, value):
        from clytie.sdk.behavior import Respond
        return Respond()
    async def passthrough(self, value):
        return value
    async def contribute(self, request):
        from clytie.sdk.types import ContextSection
        return ContextSection(title="fictional", text="example", priority=1)
    async def service(self, method, args):
        return {"ready": True}
    async def command(self, args, api):
        response = await api.request("GET", "/api/v1/status")
        return 0 if response.status == 200 else 1
    async def route(self, args):
        return {}
"""
)


async def test_new_plugin_all_points_and_dispatch_without_core_modification(tmp_path):
    from pathlib import Path

    template = json.loads(
        (Path(__file__).parents[1] / "plugins/echo-tool/plugin.json").read_text(encoding="utf-8")
    )["contributes"]["tools"][0]
    tool = dict(template, factory="create_tool", name="new-tool")
    contributions = {
        "model": {"factory": "create_model"},
        "channel": {"factory": "create_channel", "platform": "fictional"},
        "embedder": {"factory": "create_embedder", "dimension": 3},
        "tools": [tool],
        "memory": {
            "retriever": {"factory": "create_retriever"},
            "importer": {"factory": "create_importer", "formats": ["jsonl"]},
            "consolidator": {"factory": "create_consolidator"},
        },
        "notifier": {"factory": "create_notifier"},
        "behavior": {
            "message_received": {"handler": "observe"},
            "before_attention": {"handler": "before_attention"},
            "attention": {"handler": "attention"},
            "after_generation": {"handler": "passthrough"},
            "before_send": {"handler": "passthrough"},
            "task_completed": {"handler": "observe"},
        },
        "context_section": {"handler": "contribute", "budget_chars": 30},
        "schedule": [{"id": "tick", "every": "PT1M", "handler": "observe"}],
        "services": [
            {"name": "new.state", "version": "1.0", "methods": ["get"], "handler": "service"}
        ],
        "events": {
            "subscribe": ["plugin.*"],
            "handler": "on_event",
            "publish": ["new-plugin.changed"],
        },
        "cli_commands": [
            {"name": "example", "help": "fictional", "args": {}, "handler": "command"}
        ],
        "api_routes": [
            {
                "method": "GET",
                "path": "/plugins/new-plugin/status",
                "handler": "route",
                "audit": False,
            }
        ],
        "webui_panels": [
            {
                "id": "state",
                "title": "fictional",
                "kind": "kv",
                "data_route": "/plugins/new-plugin/status",
            }
        ],
    }
    plugin(tmp_path, "new-plugin", SOURCE_ALL, contributes=contributions)
    h = host(tmp_path, backends=lambda cid, pid: {"identity": FakeIdentity(pid)})
    runtime = await enable(h, "new-plugin")
    expected = {
        "channel",
        "model",
        "embedder",
        "tool",
        "memory.retriever",
        "memory.importer",
        "memory.consolidator",
        "notifier",
        "context_section",
        "schedule",
        "service",
        "event_subscriber",
        "cli_command",
        "api_route",
        "webui_panel",
        "config",
    } | {"behavior." + p for p in contributions["behavior"]}
    assert {r.point for r in h.registrations("companion_a")} == expected

    async def call(point, key, *args, method=None):
        return await h.invoke("companion_a", "new-plugin", point, key, *args, method=method)

    assert (
        await call("model", "model", sample_request(h.clock), CancelToken(), method="generate")
    ).finish == "stop"
    assert await call("model", "model", method="quota") == "unknown"
    assert await call("channel", "channel", method="connect") is None
    assert await call("channel", "channel", sample_intent(), CancelToken(), method="send")
    assert await call(
        "channel", "channel", sample_intent(react=True), CancelToken(), method="react"
    )
    feed = await call("channel", "channel", method="events")
    assert len([event async for event in feed]) == 1
    assert await call("channel", "channel", method="close") is None
    assert (
        len(await call("embedder", "embedder", ["fictional"], CancelToken(), method="embed")) == 1
    )
    tool_call = sample_call().model_copy(update={"tool": "new-tool"})
    assert (
        await h.execute_tool(
            "companion_a", "new-tool", tool_call, sample_invocation(), CancelToken()
        )
    ).status == "ok"
    reader = FakeCandidateReader([sample_memory(h.clock)], clock=h.clock)
    query = sample_query().model_copy(update={"text": "fictional"})
    assert await call(
        "memory.retriever", "retriever", query, reader, CancelToken(), method="search"
    )
    assert await call("memory.importer", "importer", tmp_path / "example.jsonl", method="detect")
    assert await call(
        "memory.importer", "importer", tmp_path / "example.jsonl", CancelToken(), method="read"
    )
    assert await call(
        "memory.consolidator",
        "consolidator",
        __import__("clytie.sdk.types", fromlist=["RecollectWindow"]).RecollectWindow(
            scene="owner_private", conversation_kind="private", messages=[]
        ),
        reader,
        CancelToken(),
        method="run",
    )
    assert (
        await call(
            "notifier",
            "notifier",
            Notice(level="info", title="fictional", body="example", dedupe_key="example"),
            CancelToken(),
            method="notify",
        )
    ).status == "sent"
    event = sample_event(h.clock)
    await h.behavior("companion_a", "message_received", event)
    assert (
        await h.behavior(
            "companion_a", "before_attention", (event, h.companion_state("companion_a"))
        )
    ).type == "normal"
    assert (await h.behavior("companion_a", "attention", (event, []))).type == "respond"
    draft = ReplyDraft(
        parts=sample_intent().parts,
        conversation=sample_conversation(),
        trace_id="a" * 32,
        form="plain",
    )
    assert await h.behavior("companion_a", "after_generation", draft) == draft
    assert await h.behavior("companion_a", "before_send", [sample_intent()]) == [sample_intent()]
    await h.behavior("companion_a", "task_completed", sample_task_summary(h.clock))
    request = ContextRequest(scene="public_group", actor=sample_actor(), budget_chars=30)
    assert len(await h.context_sections("companion_a", request)) == 1
    await h.dispatch_tick(
        "companion_a",
        "new-plugin",
        "tick",
        Tick(schedule_id="tick", scheduled_at=h.clock.now(), now=h.clock.now(), missed=0),
    )
    assert await call("service", "new.state", "get", {}) == {"ready": True}
    assert await call("cli_command", "example", {}, FakeApiClient(clock=h.clock)) == 0
    await runtime.ctx.events.publish("new-plugin.changed", {})
    with pytest.raises(PluginFailure, match="not_assembled"):
        await call("api_route", "/plugins/new-plugin/status", {})
    assert runtime.plugin.seen
    await h.close()


async def test_desktop_notification_uses_fixed_script_and_fake_backend_only(tmp_path):
    calls = []

    async def run(args, **kwargs):
        from clytie.sdk.types import ProcessResult

        calls.append((args, kwargs))
        return ProcessResult(returncode=0, stdout=b"fictional", stderr=b"", truncated=False)

    h = host(tmp_path, backends=lambda cid, pid: {"subprocess": SimpleNamespace(run=run)})
    runtime = await enable(h, "desktop-notifier")
    title = 'fictional; $(arbitrary-command) " <xml>'
    receipt = await h.invoke(
        "companion_a",
        "desktop-notifier",
        "notifier",
        "notifier",
        Notice(level="warn", title=title, body=title, dedupe_key="example"),
        CancelToken(),
        method="notify",
    )
    assert receipt.status == "sent" and len(calls) == 1
    args, options = calls[0]
    assert title not in " ".join(args)
    assert options["env"] == {"CLYTIE_NOTICE_TITLE": title, "CLYTIE_NOTICE_BODY": title}
    assert options["cwd"] == runtime.storage.path and options["shell"] is False
    await h.close()
