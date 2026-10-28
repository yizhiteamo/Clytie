from pathlib import Path

from clytie.plugin_host import PluginHost
from clytie.sdk.testing import MockClock

from .test_host_discovery import make_manifest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = """
class Plugin:
    async def start(self, ctx):
        self.ctx = ctx
        self.seen = []
        await ctx.clock.sleep(0)
    async def stop(self):
        await self.ctx.clock.sleep(0)
    async def on_event(self, event):
        self.seen.append(event)
        await self.ctx.clock.sleep(0)
"""


def plugin(home, plugin_id, source=SOURCE, **overrides):
    path = make_manifest(home / "plugins", plugin_id, **overrides)
    (path.parent / "plugin.py").write_text(source, encoding="utf-8")
    return path


def host(tmp_path, *, backends=None, clock=None):
    result = PluginHost(ROOT, tmp_path, clock=clock or MockClock(), backends=backends)
    result.configure("companion_a")
    return result


async def enable(h, plugin_id, cid="companion_a"):
    plan = h.plan_enable(cid, plugin_id)
    assert plan.ok, plan.errors
    result = await h.apply(plan.plan_id)
    assert not result["failed"], h.failures
    return h.runtimes[cid, plugin_id]
