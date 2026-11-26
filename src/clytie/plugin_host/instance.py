"""实例范围 notifier：复用发现、入口加载和 factory 装配，不提供伴侣资源。"""

from types import SimpleNamespace

from clytie.config import freeze

from .host import Runtime
from .registry import assemble
from .support import timed
from .surface import surface


class InstanceNotifiers:
    def __init__(self, host, *, subprocess_backend=None):
        self.host, self.backend = host, subprocess_backend
        self.runtimes = []
        self.by_id = {}

    @property
    def notifiers(self):
        return [entry for entry in self.by_id.values() if entry[0] is not None]

    async def start(self, ids, configs=None):
        configs = configs or {}
        for pid in ids:
            manifest = self.host.manifests[pid]
            if "notifier" not in manifest.data["contributes"]:
                raise ValueError("notifier required")
            outward = bool(manifest.data["contributes"]["notifier"].get("outward", False))
            self.by_id[pid] = (None, outward)
            runtime = Runtime("", manifest, None, manifest.priority)
            try:
                cls, runtime.module_name = self.host._load_class(manifest)
                config = self.host._config("", manifest, configs.get(pid, {}).get("config", {}))
                values = dict(config=freeze(config), clock=self.host.clock, log=self.host.log)
                if self.backend is not None and "subprocess" in manifest.data.get(
                    "permissions", ()
                ):
                    values["subprocess"] = surface(self.backend, {"run"})
                runtime.ctx = surface(SimpleNamespace(**values), values)
                runtime.plugin = cls()
                await timed(runtime.plugin.start(runtime.ctx), self.host.clock, 10)
                runtime.registrations = assemble(runtime, scope="instance")
                self.runtimes.append(runtime)
                for registration in runtime.registrations:
                    self.by_id[pid] = (registration.implementation, outward)
            except Exception:
                if runtime.plugin:
                    try:
                        await timed(runtime.plugin.stop(), self.host.clock, 10)
                    except Exception:
                        pass
                self.host.log.warning("instance.notifier_failed", plugin_id=pid)

    async def close(self):
        runtimes, self.runtimes = self.runtimes, []
        self.by_id.clear()
        for runtime in reversed(runtimes):
            try:
                await timed(runtime.plugin.stop(), self.host.clock, 10)
            except Exception:
                self.host.log.warning(
                    "instance.notifier_stop_failed", plugin_id=runtime.manifest.id
                )
