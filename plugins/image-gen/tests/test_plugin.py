import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from clytie.sdk import CancelToken
from clytie.sdk.immutable import FrozenDict
from clytie.sdk.testing import FakeContext, MockClock, assert_capability_contract
from clytie.sdk.testing.samples import sample_call, sample_invocation
from clytie.sdk.types import Denial


def test_manifest_examples_distinguish_draw_from_edit():
    manifest = json.loads((Path(__file__).parents[1] / "plugin.json").read_text(encoding="utf-8"))
    tools = {tool["name"]: tool for tool in manifest["contributes"]["tools"]}
    draw, edit = tools["draw_image"], tools["edit_image"]
    assert draw["examples"] != edit["examples"]
    assert [example["input"] for example in edit["examples"] if example["call"]] == [
        "把刚才那张改成红色"
    ]
    negatives = {example["input"] for example in edit["examples"] if not example["call"]}
    assert {"重新画一只猫（新画，用 draw_image）", "不要修改刚才那张图"} <= negatives
    assert "修改刚生成的图时用 edit_image" in draw["when_not_to_use"]


def load_plugin():
    path = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location(
        "test_image_plugin", path / "plugin.py", submodule_search_locations=[str(path)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def setup(tmp_path):
    module = load_plugin()
    ctx = FakeContext(root=tmp_path, clock=MockClock(), permissions=["assets.write"])
    ctx.config = FrozenDict({"backends": []})
    plugin = module.Plugin()
    await plugin.start(ctx)
    return module, ctx, plugin


async def test_render_service_reads_references_and_returns_product_without_edit_history(tmp_path):
    _, ctx, plugin = await setup(tmp_path)
    inv = sample_invocation()
    token = CancelToken()
    source = await ctx.asset_writer.put(b"sample reference", "image/png", "fictional")

    async def render(prompt, images, size, cancel):
        assert cancel is token
        assert images == [(b"sample reference", "image/png")]
        assert prompt == "fictional scene" and size == "1024x1536"
        return b"sample page", "image/png", {"input_tokens": 7}

    plugin.backends = [SimpleNamespace(render=render)]
    args = {
        "prompt": "fictional scene",
        "size": "1024x1536",
        "references": [source.model_dump(mode="json")],
    }
    result = await plugin.render_service("render", args, SimpleNamespace(cancel=token))
    assert await ctx.assets.read(result["asset"]["asset_id"]) == b"sample page"
    assert result["usage"] == {"input_tokens": 7}
    assert await ctx.storage.get(plugin.history_key(inv)) is None


class FakeBackend:
    def __init__(self, ctx, error=None):
        self.ctx, self.error, self.calls = ctx, error, []

    async def generate(self, prompt, cancel):
        cancel.raise_if_cancelled()
        await self.ctx.clock.sleep(0)
        self.calls.append(prompt)
        if self.error:
            raise self.error
        return b"fictional-image", "image/png"

    async def edit(self, prompt, data, media_type, cancel):
        assert data == b"fictional-image" and media_type == "image/png"
        return await self.generate(prompt, cancel)


@pytest.mark.parametrize("proactive", [False, True])
async def test_tools_contract_edit_only_same_conversation_and_history(tmp_path, proactive):
    _, ctx, plugin = await setup(tmp_path)
    backend = FakeBackend(ctx)
    plugin.backends = [backend]
    call = sample_call().model_copy(update={"arguments": {"prompt": "fictional bird"}})
    inv = sample_invocation()
    if proactive:
        inv = inv.model_copy(update={"actor": None, "origin": "proactive"})
    draw, edit = plugin.create_draw(), plugin.create_edit()
    assert isinstance(await edit.prepare(call, inv), Denial)
    await assert_capability_contract(draw, call, inv)
    await assert_capability_contract(edit, call, inv)
    other = inv.model_copy(
        update={"conversation": inv.conversation.model_copy(update={"conversation_id": "other"})}
    )
    assert isinstance(await edit.prepare(call, other), Denial)
    assert isinstance(await draw.prepare(call.model_copy(update={"arguments": {}}), inv), Denial)
    for _ in range(7):
        result = await draw.execute(await draw.prepare(call, inv), inv, CancelToken())
        assert result.status == "ok" and result.assets
    assert len(await ctx.storage.get(plugin.history_key(inv))) == 5
    await plugin.stop()


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize(
    "code", ["content_refused", "rate_limited", "transient", "quota_exceeded", "invalid_request"]
)
async def test_fallback_only_explicit_refusal(tmp_path, code, fallback):
    module, ctx, plugin = await setup(tmp_path)
    first = FakeBackend(ctx, module.BackendError(code))
    second = FakeBackend(ctx)
    plugin.backends = [first, second]
    plugin.fallback = fallback
    call = sample_call().model_copy(update={"arguments": {"prompt": "fictional"}})
    inv = sample_invocation()
    tool = plugin.create_draw()
    result = await tool.execute(await tool.prepare(call, inv), inv, CancelToken())
    passed = fallback and code == "content_refused"
    assert bool(second.calls) == passed
    assert result.status == ("ok" if passed else "error")
    if not passed:
        assert result.error.code == code and code in result.content


async def test_edit_plan_rechecks_last_image(tmp_path):
    _, ctx, plugin = await setup(tmp_path)
    backend = FakeBackend(ctx)
    plugin.backends = [backend]
    inv, call = (
        sample_invocation(),
        sample_call().model_copy(update={"arguments": {"prompt": "fictional"}}),
    )
    draw, edit = plugin.create_draw(), plugin.create_edit()
    await draw.execute(await draw.prepare(call, inv), inv, CancelToken())
    plan = await edit.prepare(call, inv)
    await ctx.storage.set(plugin.history_key(inv), [{"asset_id": "b" * 64}])
    result = await edit.execute(plan, inv, CancelToken())
    assert result.error.code == "invalid_request" and len(backend.calls) == 1


async def test_render_limit_independent_of_serial_draw_and_edit(tmp_path):
    _, ctx, plugin = await setup(tmp_path)
    release_pages = asyncio.Event()
    release_edit = asyncio.Event()
    active_pages = set()
    calls = []

    class Backend(FakeBackend):
        async def render(self, prompt, images, size, cancel):
            active_pages.add(prompt)
            calls.append(prompt)
            try:
                await release_pages.wait()
                return b"fictional-page", "image/png", {}
            finally:
                active_pages.remove(prompt)

        async def edit(self, prompt, data, media_type, cancel):
            calls.append("edit")
            await release_edit.wait()
            return data, media_type

    backend = Backend(ctx)
    plugin.backends = [backend]
    inv = sample_invocation()
    call = sample_call().model_copy(update={"arguments": {"prompt": "fictional"}})
    draw, edit = plugin.create_draw(), plugin.create_edit()
    plan = await draw.prepare(call, inv)
    assert (await draw.execute(plan, inv, CancelToken())).status == "ok"
    backend.calls.clear()
    edit_plan = await edit.prepare(call, inv)

    def render(index, token):
        args = {"prompt": f"page-{index}", "size": "1024x1536"}
        return asyncio.create_task(
            plugin.render_service("render", args, SimpleNamespace(cancel=token))
        )

    tokens = [CancelToken() for _ in range(6)]
    pages = [render(i, token) for i, token in enumerate(tokens)]
    editing = asyncio.create_task(edit.execute(edit_plan, inv, CancelToken()))
    drawing = asyncio.create_task(draw.execute(plan, inv, CancelToken()))
    try:
        await ctx.clock.drain()
        assert len(active_pages) == 4
        assert calls.count("edit") == 1 and not backend.calls

        tokens[4].cancel()
        pages[1].cancel()
        await ctx.clock.drain()
        assert pages[4].cancelled() and pages[1].cancelled()
        assert active_pages == {"page-0", "page-2", "page-3", "page-5"}
        assert "page-4" not in calls

        release_edit.set()
        await ctx.clock.drain()
        assert editing.result().status == drawing.result().status == "ok"
        assert backend.calls == ["fictional"]
        assert len(active_pages) == 4
    finally:
        release_edit.set()
        release_pages.set()
        results = await asyncio.gather(*pages, editing, drawing, return_exceptions=True)
    assert all("asset" in results[index] for index in (0, 2, 3, 5))


@pytest.mark.parametrize("error", [TimeoutError(), OSError()])
async def test_render_timeout_is_transient_without_retry_or_fallback(tmp_path, error):
    module, ctx, plugin = await setup(tmp_path)
    calls = []

    async def request(method, url, **kwargs):
        calls.append(kwargs["timeout_s"])
        raise error

    async def read(ref):
        return "fictional-value"

    ctx.http = SimpleNamespace(request=request)
    ctx.credentials = SimpleNamespace(read=read)
    config = {"model": "fictional", "api_key_ref": "fictional"}
    plugin.backends = [module.OpenAIBackend(ctx, config), module.OpenAIBackend(ctx, config)]
    plugin.fallback = True
    result = await plugin.render_service(
        "render",
        {"prompt": "fictional", "size": "1024x1536"},
        SimpleNamespace(cancel=CancelToken()),
    )
    assert result == {"error": "transient"}
    assert calls == [300]
