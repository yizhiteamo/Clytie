"""供离线契约测试使用的清单装配器，不是生产插件宿主。"""

import asyncio
import inspect
import json
import re
from contextlib import asynccontextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from .context_fakes import FakeContext


async def with_clock_timeout(operation, clock, seconds):
    """超时只用注入时钟；模拟时钟由测试驱动。"""
    task = asyncio.create_task(operation)
    timer = asyncio.create_task(clock.sleep(seconds))
    try:
        done, _ = await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return await task
        raise TimeoutError("plugin operation exceeded its clock budget")
    finally:
        for pending in (task, timer):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, timer, return_exceptions=True)


class LoadedPlugin:
    def __init__(self, manifest, plugin):
        self.manifest, self.plugin = manifest, plugin
        self.active = True

    def _member(self, point, index, field):
        if not self.active:
            raise RuntimeError("plugin is not started")
        declaration = self.manifest["contributes"]
        for part in point.split("."):
            declaration = declaration[part]
        if isinstance(declaration, list):
            declaration = declaration[index]
        name = declaration[field]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("handler/factory must be an instance attribute name")
        member = getattr(self.plugin, name)
        if not callable(member):
            raise TypeError("declared attribute is not callable")
        return member

    def factory(self, point, index=0):
        result = self._member(point, index, "factory")()
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("factory must be synchronous")
        return result

    def handler(self, point, index=0):
        member = self._member(point, index, "handler")
        if not inspect.iscoroutinefunction(member):
            raise TypeError("handler must be asynchronous")
        return member


@asynccontextmanager
async def plugin_from_manifest(path, ctx=None, *, clock=None, root=None):
    path = Path(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    module_name, class_name = manifest["entry"].split(":")
    if not all(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part)
        for part in [*module_name.split("."), class_name]
    ):
        raise ValueError("invalid plugin entry")
    source = path.parent.joinpath(*module_name.split(".")).with_suffix(".py")
    if not source.resolve().is_relative_to(path.parent.resolve()):
        raise ValueError("plugin entry must remain within its directory")
    spec = spec_from_file_location("clytie_test_" + manifest["id"].replace("-", "_"), source)
    if spec is None or spec.loader is None:
        raise ValueError("plugin entry cannot be loaded")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = getattr(module, class_name)()
    ctx = ctx or FakeContext(
        plugin_id=manifest["id"],
        permissions=manifest.get("permissions", ()),
        channel="channel" in manifest["contributes"],
        clock=clock,
        root=root,
    )
    await with_clock_timeout(plugin.start(ctx), ctx.clock, 10)
    loaded = LoadedPlugin(manifest, plugin)
    try:
        yield loaded
    finally:
        loaded.active = False
        try:
            await with_clock_timeout(plugin.stop(), ctx.clock, 10)
        except Exception:
            ctx.log.error("plugin.stop_failed", plugin_id=ctx.plugin_id)


async def assert_plugin_contract(factory, ctx):
    from .helpers import assert_task_cancellation

    plugin = factory()
    assert await plugin.start(ctx) is None
    assert await plugin.stop() is None
    await assert_task_cancellation(lambda: factory().start(ctx))
    await assert_task_cancellation(plugin.stop)
