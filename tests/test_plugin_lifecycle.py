"""插件生命周期与清单装配失败回归。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from clytie.sdk.testing import FakeContext, MockClock, checkpoint
from clytie.sdk.testing.lifecycle import assert_plugin_contract, plugin_from_manifest


def manifest(tmp_path, monkeypatch, plugin):
    from clytie.sdk.testing import lifecycle

    path = tmp_path / "plugin.json"
    path.write_text(
        json.dumps(
            {
                "id": "fake-plugin",
                "entry": "plugin:Plugin",
                "contributes": {
                    "model": {"factory": "create_model"},
                    "behavior": {"message_received": {"handler": "on_event"}},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        lifecycle,
        "spec_from_file_location",
        lambda *args: SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None)),
    )
    monkeypatch.setattr(lifecycle, "module_from_spec", lambda spec: SimpleNamespace(Plugin=plugin))
    return path


class GoodPlugin:
    def __init__(self):
        self.order = ["construct"]

    async def start(self, ctx):
        self.ctx = ctx
        await ctx.clock.sleep(0)
        self.order.append("start")

    async def stop(self):
        await self.ctx.clock.sleep(0)
        self.order.append("stop")

    def create_model(self):
        assert self.order[-1] == "start"
        self.order.append("factory")
        return object()

    async def on_event(self, event):
        pass


async def test_construct_start_factory_stop_order_and_contract(tmp_path, monkeypatch):
    path = manifest(tmp_path, monkeypatch, GoodPlugin)
    ctx = FakeContext(root=tmp_path)
    await assert_plugin_contract(GoodPlugin, ctx)
    async with plugin_from_manifest(path, ctx) as loaded:
        assert loaded.factory("model") is not None
        await loaded.handler("behavior.message_received")({})
        assert loaded.plugin.order == ["construct", "start", "factory"]
    assert loaded.plugin.order[-1] == "stop"
    with pytest.raises(RuntimeError):
        loaded.factory("model")


async def test_failed_start_never_assembles_extensions(tmp_path, monkeypatch):
    calls = []

    class Failed(GoodPlugin):
        async def start(self, ctx):
            raise ValueError("fictional startup failure")

        def create_model(self):
            calls.append("factory")

    path = manifest(tmp_path, monkeypatch, Failed)
    with pytest.raises(ValueError, match="startup"):
        async with plugin_from_manifest(path, FakeContext(root=tmp_path)):
            pytest.fail("must not yield a failed plugin")
    assert calls == []


async def test_stop_failure_is_logged_and_does_not_replace_body_error(tmp_path, monkeypatch):
    class FailedStop(GoodPlugin):
        async def stop(self):
            raise RuntimeError("fictional stop failure")

    path = manifest(tmp_path, monkeypatch, FailedStop)
    ctx = FakeContext(root=tmp_path)
    with pytest.raises(ValueError, match="body"):
        async with plugin_from_manifest(path, ctx):
            raise ValueError("body error")
    assert ctx.log.entries[-1][1] == "plugin.stop_failed"


@pytest.mark.parametrize("phase", ["start", "stop"])
async def test_lifecycle_timeout_uses_simulated_clock(tmp_path, monkeypatch, phase):
    class Stuck(GoodPlugin):
        async def start(self, ctx):
            self.ctx = ctx
            if phase == "start":
                await ctx.clock.sleep(20)

        async def stop(self):
            if phase == "stop":
                await self.ctx.clock.sleep(20)

    clock = MockClock()
    ctx = FakeContext(root=tmp_path, clock=clock)
    path = manifest(tmp_path, monkeypatch, Stuck)

    async def run():
        async with plugin_from_manifest(path, ctx):
            pass

    task = asyncio.create_task(run())
    for _ in range(12):
        await checkpoint()
    clock.advance(10)
    if phase == "start":
        with pytest.raises(TimeoutError):
            await task
    else:
        await task
        assert ctx.log.entries[-1][1] == "plugin.stop_failed"


async def test_async_factory_is_rejected(tmp_path, monkeypatch):
    class InvalidFactory(GoodPlugin):
        async def create_model(self):
            return object()

    path = manifest(tmp_path, monkeypatch, InvalidFactory)
    async with plugin_from_manifest(path, FakeContext(root=tmp_path)) as loaded:
        with pytest.raises(TypeError, match="synchronous"):
            loaded.factory("model")
