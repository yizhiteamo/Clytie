import importlib.util
import sys
from pathlib import Path

import pytest

from clytie.sdk import CancelToken
from clytie.sdk.immutable import FrozenDict
from clytie.sdk.testing import FakeContext, MockClock, assert_capability_contract
from clytie.sdk.testing.samples import sample_call, sample_invocation
from clytie.sdk.types import Denial


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


async def test_tools_contract_edit_only_same_conversation_and_history(tmp_path):
    _, ctx, plugin = await setup(tmp_path)
    backend = FakeBackend(ctx)
    plugin.backends = [backend]
    call = sample_call().model_copy(update={"arguments": {"prompt": "fictional bird"}})
    inv = sample_invocation()
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
