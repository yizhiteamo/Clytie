"""按伴侣装配、计划启停、隔离分发和升级的插件宿主。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import shutil
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from clytie.config import _default_config, _Reader, deep_merge, thaw
from clytie.sdk import CancelToken
from clytie.sdk.behavior import (
    collect_context_sections,
    merge_attention,
    merge_before_attention,
    valid_after_generation,
    valid_before_send,
)
from clytie.sdk.types import (
    BusEvent,
    CompanionState,
    Denial,
    ModelTurn,
    Receipt,
    TokenUsage,
    ToolResult,
)

from .context import BackendUnavailable, build_context
from .dependencies import DependencyError, resolve
from .manifests import discover, read_manifest
from .memory import bind_scene
from .registry import assemble, validate_result
from .snapshots import UpgradeSnapshots, atomic_write, canonical, contained, identifier
from .support import FileStorage, HostLog, json_copy, timed

CORE_MODULES = frozenset(
    {
        "kernel",
        "agent",
        "memory",
        "persona",
        "boundary",
        "plugin-host",
        "plugin_host",
        "scheduler",
        "config",
        "assets",
        "supervisor",
        "api",
        "webui",
        "cli",
    }
)


class PluginFailure(RuntimeError):
    """内部故障值不包含可能携带私密正文的异常字符串。"""


class PlanStale(ValueError):
    pass


@dataclass(frozen=True)
class HostPlan:
    plan_id: str
    operation: str
    companion_id: str
    target: str
    steps: tuple[str, ...]
    errors: tuple[str, ...]
    fingerprint: str
    restore_cascaded: bool = False
    previously_cascaded: tuple[str, ...] = ()

    @property
    def ok(self):
        return not self.errors


@dataclass
class Runtime:
    companion_id: str
    manifest: object
    storage: FileStorage
    priority: int
    plugin: object = None
    ctx: object = None
    registrations: tuple = ()
    status: str = "starting"
    module_name: str = ""
    calls: set = field(default_factory=set)
    units: list = field(default_factory=list)
    unit_role: str | None = None
    account_ref: str | None = None


class PluginHost:
    failure_type = PluginFailure

    def __init__(self, repo_root, home, *, clock, backends=None, log=None, seed=0):
        self.repo_root, self.home = Path(repo_root).resolve(), Path(home).resolve()
        self.clock, self.log, self.seed = clock, log or HostLog(), seed
        self.backends = backends or (lambda cid, pid: {})
        self.manifests = discover(
            self.repo_root / "plugins", self.home / "plugins", repo_root=self.repo_root
        )
        self._excluded_file = contained(
            self.home, self.home / "plugins" / ".host" / "uninstalled.json"
        )
        self._excluded = (
            set(json.loads(self._excluded_file.read_text(encoding="utf-8")))
            if self._excluded_file.exists()
            else set()
        )
        self.manifests = {pid: m for pid, m in self.manifests.items() if pid not in self._excluded}
        self.runtimes = {}
        self.settings, self.private_scenes, self.states = {}, {}, {}
        self.unit_configs, self.identities = {}, {}
        self.events, self.failures = [], []
        self.snapshots = UpgradeSnapshots(self.home, clock, self.log)
        self._plans, self._cascades, self._storages = {}, {}, {}
        self._queue, self._dispatching = deque(), False
        self._serial = 0
        self._lock = asyncio.Lock()

    def configure(
        self,
        companion_id,
        *,
        plugins=None,
        instance_plugins=None,
        private_scenes=("owner_private",),
    ):
        identifier(companion_id)
        self.settings[companion_id] = json_copy(
            {"plugins": plugins or {}, "instance": instance_plugins or {}}
        )
        self.private_scenes[companion_id] = tuple(private_scenes)

    def is_active(self, companion_id, plugin_id, _seen=None):
        seen = set() if _seen is None else set(_seen)
        if plugin_id in seen:
            return False
        seen.add(plugin_id)
        runtime = self.runtimes.get((companion_id, plugin_id))
        return (
            runtime is not None
            and runtime.status == "active"
            and all(
                self.is_active(companion_id, dep, seen)
                for dep, spec in runtime.manifest.data.get("dependencies", {}).items()
                if not dep.startswith("x-") and not spec.get("optional", False)
            )
        )

    def configure_report(self, report):
        if not report.valid:
            from .manifests import ManifestError

            raise ManifestError(list(report.errors))
        from .identity import IdentityResolver

        for cid, result in report.plugin_configs.items():
            config = result["config"]
            self.configure(
                cid,
                plugins=thaw(config.get("plugins", {})),
                instance_plugins=thaw(report.instance.get("plugins", {})),
                private_scenes=config.get("memory", {}).get("private_scenes", ("owner_private",)),
            )
            for old_key in tuple(self.unit_configs):
                if old_key[0] == cid:
                    self.unit_configs.pop(old_key)
            model = config["model"]
            self.unit_configs[cid, model["plugin"]] = [{"role": "model", "config": result["model"]}]
            for index, channel in enumerate(config["channels"]):
                pid, ref = channel["plugin"], channel["account"]
                self.unit_configs.setdefault((cid, pid), []).append(
                    {
                        "role": "channel",
                        "config": result["channels"][index],
                        "account": ref,
                    }
                )
                self.identities[cid, pid, ref] = IdentityResolver(ref, pid, report.instance)

    def _active(self, companion_id):
        return sorted(
            pid
            for (cid, pid), r in self.runtimes.items()
            if cid == companion_id and r.status == "active"
        )

    def _metadata(self, companion_id, name):
        return contained(
            self.home,
            self.home / "companions" / identifier(companion_id) / "plugin-data" / "_host" / name,
        )

    def _history(self, companion_id):
        if companion_id not in self._cascades:
            file = self._metadata(companion_id, "cascades.json")
            self._cascades[companion_id] = (
                json.loads(file.read_text(encoding="utf-8")) if file.exists() else []
            )
        return self._cascades[companion_id]

    def _previous(self, companion_id, target):
        records = [r for r in self._history(companion_id) if r["target"] == target]
        if not records:
            return ()
        group = records[-1]["operation"]
        return tuple(r["plugin"] for r in records if r["operation"] == group)

    def _fingerprint(self, companion_id):
        state = {
            "manifests": {pid: thaw(m.data) for pid, m in sorted(self.manifests.items())},
            "active": self._active(companion_id),
            "settings": self.settings.get(companion_id),
            "failed": sorted(
                pid
                for (cid, pid), r in self.runtimes.items()
                if cid == companion_id and r.status == "start_failed"
            ),
        }
        return hashlib.sha256(canonical(state)).hexdigest()

    def _plan(self, operation, companion_id, target, restore=False):
        identifier(companion_id)
        fingerprint, errors, steps = self._fingerprint(companion_id), [], ()
        previous = self._previous(companion_id, target)
        if target in CORE_MODULES:
            errors.append("core_module: core modules cannot be disabled or uninstalled")
        elif target not in self.manifests:
            errors.append(f"missing plugin {target}; install it before retrying")
        elif operation == "enable":
            requested = [target, *(previous if restore else ())]
            try:
                order = resolve(self.manifests, requested, active=self._active(companion_id)).order
                steps = tuple(pid for pid in order if not self.is_active(companion_id, pid))
                # 同一伴侣中同名服务/工具不能静默覆盖。
                names = set()
                for pid in order:
                    for category, field in (("service", "services"), ("tool", "tools")):
                        for item in self.manifests[pid].data["contributes"].get(field, ()):
                            key = (category, item["name"])
                            if key in names:
                                errors.append(f"conflicting {category}: {item['name']}")
                            names.add(key)
            except DependencyError as exc:
                errors.append(str(exc))
        else:
            if operation == "uninstall" and any(
                cid != companion_id and pid == target and runtime.status == "active"
                for (cid, pid), runtime in self.runtimes.items()
            ):
                errors.append("plugin active in another companion; confirm its disable plan first")
            active = self._active(companion_id)
            depths = {target: 0}
            for _ in range(len(active)):
                changed = False
                for pid in active:
                    deps = self.runtimes[companion_id, pid].manifest.data.get("dependencies", {})
                    levels = [
                        depths[d] + 1
                        for d, s in deps.items()
                        if d in depths and not s.get("optional", False)
                    ]
                    if levels and max(levels) > depths.get(pid, -1):
                        depths[pid], changed = max(levels), True
                if not changed:
                    break
            else:
                errors.append("dependency cycle; refresh manifests before disabling")
            steps = tuple(
                sorted((pid for pid in depths if pid in active), key=lambda p: (-depths[p], p))
            )
        payload = {
            "operation": operation,
            "companion": companion_id,
            "target": target,
            "restore": restore,
            "steps": steps,
            "errors": errors,
            "fingerprint": fingerprint,
            "previous": previous,
        }
        plan_id = hashlib.sha256(canonical(payload)).hexdigest()[:32]
        plan = HostPlan(
            plan_id,
            operation,
            companion_id,
            target,
            steps,
            tuple(errors),
            fingerprint,
            restore,
            previous,
        )
        self._plans[plan_id] = plan
        return plan

    def plan_enable(self, companion_id, target, *, restore_cascaded=False):
        self.refresh()
        return self._plan("enable", companion_id, target, restore_cascaded)

    def plan_disable(self, companion_id, target):
        self.refresh()
        return self._plan("disable", companion_id, target)

    def plan_uninstall(self, companion_id, target):
        self.refresh()
        return self._plan("uninstall", companion_id, target)

    async def apply(self, plan_id):
        async with self._lock:
            self.refresh()
            plan = self._plans.get(plan_id)
            if (
                plan is None
                or self._plan(
                    plan.operation, plan.companion_id, plan.target, plan.restore_cascaded
                ).plan_id
                != plan_id
            ):
                raise PlanStale("plan_stale: regenerate and confirm the plan")
            if not plan.ok:
                raise DependencyError("; ".join(plan.errors))
            enabled, stopped, failed = [], [], []
            for pid in plan.steps:
                if plan.operation == "enable":
                    if self.is_active(plan.companion_id, pid):
                        continue
                    if await self._start(plan.companion_id, self.manifests[pid]):
                        enabled.append(pid)
                    else:
                        failed.append(pid)
                else:
                    reason = (
                        "cascade"
                        if pid != plan.target
                        else "uninstalled"
                        if plan.operation == "uninstall"
                        else "requested"
                    )
                    await self._stop(plan.companion_id, pid, reason)
                    stopped.append(pid)
                    if pid != plan.target:
                        record = {
                            "target": plan.target,
                            "plugin": pid,
                            "at": self.clock.now().isoformat(),
                            "operation": plan_id,
                        }
                        history = self._history(plan.companion_id)
                        history.append(record)
                        atomic_write(
                            self._metadata(plan.companion_id, "cascades.json"), canonical(history)
                        )
                        self.log.info("plugin.cascade_stopped", **record)
            if plan.operation == "uninstall":
                self._excluded.add(plan.target)
                atomic_write(self._excluded_file, canonical(sorted(self._excluded)))
                self.refresh()
            return {
                "enabled": enabled,
                "stopped": stopped,
                "failed": failed,
                "patch": {
                    "plugins": {
                        pid: {"enabled": plan.operation == "enable"} for pid in enabled + stopped
                    }
                },
            }

    def _config(self, companion_id, manifest, override=None):
        settings = self.settings.get(companion_id, {})
        local = settings.get("plugins", {}).get(manifest.id, {})
        instance = settings.get("instance", {}).get(manifest.id, {})
        schema = thaw(manifest.data["config"])
        defaults, _ = _default_config(schema, manifest.path)
        result = deep_merge(
            defaults, instance.get("config", {}), local.get("config", {}), override or {}
        )
        reader = _Reader(self.repo_root)
        reader.validate(thaw(result), schema, manifest.path, ("config",))
        if reader.errors:
            from .manifests import ManifestError

            raise ManifestError(reader.errors)
        return result

    def _load_class(self, manifest):
        self._serial += 1
        namespace = f"_clytie_plugin_{self._serial}_{hashlib.sha256(str(manifest.path).encode()).hexdigest()[:16]}"
        package = ModuleType(namespace)
        package.__path__ = [str(manifest.path.parent)]
        sys.modules[namespace] = package
        module_name, class_name = manifest.data["entry"].split(":")
        source = contained(
            manifest.path.parent,
            manifest.path.parent.joinpath(*module_name.split(".")).with_suffix(".py"),
        )
        module = ModuleType(namespace + "." + module_name)
        module.__file__, module.__package__ = str(source), namespace
        sys.modules[module.__name__] = module
        exec(compile(source.read_bytes(), str(source), "exec"), module.__dict__)
        return getattr(module, class_name), namespace

    def _record_version(self, companion_id, manifest):
        atomic_write(
            self._metadata(companion_id, manifest.id + ".json"),
            canonical(
                {
                    "plugin_id": manifest.id,
                    "data_version": manifest.data.get("data", {}).get("version", 1),
                    "plugin_version": manifest.data["version"],
                }
            ),
        )

    async def _start(self, companion_id, manifest, *, cls=None, override=None, version_check=True):
        plugin_id = manifest.id
        key = companion_id, plugin_id
        path = self.snapshots.state_path(*key)
        try:
            for dependency, spec in manifest.data.get("dependencies", {}).items():
                if (
                    not dependency.startswith("x-")
                    and not spec.get("optional", False)
                    and not self.is_active(companion_id, dependency)
                ):
                    raise ValueError("dependency_unavailable")
            record = self._metadata(companion_id, plugin_id + ".json")
            data_version = manifest.data.get("data", {}).get("version", 1)
            if version_check:
                if record.exists():
                    version = json.loads(record.read_text(encoding="utf-8"))
                    if (
                        version["data_version"] != data_version
                        or version.get("plugin_version") != manifest.data["version"]
                    ):
                        raise ValueError("data_version_changed: use explicit upgrade")
                elif path.exists() and any(path.iterdir()):
                    raise ValueError("data_version_unknown")
            storage = self._storages.setdefault(key, FileStorage(path, self.clock))
            priority = (
                self.settings.get(companion_id, {})
                .get("plugins", {})
                .get(plugin_id, {})
                .get("priority", manifest.priority)
            )
            runtime = Runtime(companion_id, manifest, storage, priority)
            self.runtimes[key] = runtime
            if cls is None:
                cls, runtime.module_name = self._load_class(manifest)
            if key in self.unit_configs:
                layouts = self.unit_configs[key]
            else:
                layouts = [{"role": None, "config": self._config(companion_id, manifest, override)}]
            for index, layout in enumerate(layouts):
                unit = runtime if index == 0 else Runtime(companion_id, manifest, storage, priority)
                runtime.units.append(unit)
                unit.unit_role, unit.account_ref = layout["role"], layout.get("account")
                backends = dict(self.backends(*key))
                if (companion_id, plugin_id, unit.account_ref) in self.identities:
                    backends["identity"] = self.identities[
                        companion_id, plugin_id, unit.account_ref
                    ]
                unit.ctx = build_context(
                    self, unit, self._config(companion_id, manifest, layout["config"]), backends
                )
                unit.plugin = cls()
                with bind_scene():
                    await timed(unit.plugin.start(unit.ctx), self.clock, 10)
            registrations = []
            for index, unit in enumerate(runtime.units):
                with bind_scene():
                    registrations.extend(assemble(unit, role=unit.unit_role, primary=index == 0))
            runtime.registrations = tuple(registrations)
            runtime.status = "active"
            if version_check:
                self._record_version(companion_id, manifest)
            await self.emit(
                companion_id,
                "plugin.enabled",
                {"plugin_id": plugin_id, "companion_id": companion_id},
            )
            return True
        except Exception as exc:
            if key not in self.runtimes:
                self.runtimes[key] = Runtime(
                    companion_id, manifest, FileStorage(path, self.clock), manifest.priority
                )
            runtime = self.runtimes[key]
            for unit in reversed(runtime.units or [runtime]):
                if unit.plugin is None:
                    continue
                try:
                    with bind_scene():
                        await timed(unit.plugin.stop(), self.clock, 10)
                except Exception as cleanup_error:
                    self._failure(runtime, "start_cleanup", cleanup_error)
            runtime.status, runtime.registrations = "start_failed", ()
            self._failure(runtime, "start", exc)
            await self.emit(
                companion_id,
                "plugin.disabled",
                {"plugin_id": plugin_id, "companion_id": companion_id, "reason": "start_failed"},
            )
            return False

    def _failure(self, runtime, point, exc):
        failure = {
            "plugin_id": runtime.manifest.id,
            "companion_id": runtime.companion_id,
            "point": point,
            "exc_type": type(exc).__name__,
            "at": self.clock.now().isoformat(),
        }
        self.failures.append(failure)
        if isinstance(exc, BackendUnavailable):
            failure["code"], failure["permission"] = "backend_unavailable", exc.permission
        self.log.error("plugin.failure", **failure)

    async def _stop(self, companion_id, plugin_id, reason="requested"):
        runtime = self.runtimes.get((companion_id, plugin_id))
        if runtime is None or runtime.status not in {"active", "quiescing"}:
            return
        runtime.status = "stopping"
        await self.emit(
            companion_id,
            "plugin.disabled",
            {"plugin_id": plugin_id, "companion_id": companion_id, "reason": reason},
        )
        try:
            if runtime.calls:
                await timed(
                    asyncio.gather(*tuple(runtime.calls), return_exceptions=True), self.clock, 10
                )
            for unit in reversed(runtime.units or [runtime]):
                try:
                    with bind_scene():
                        await timed(unit.plugin.stop(), self.clock, 10)
                except Exception as exc:
                    self._failure(runtime, "stop", exc)
        except Exception as exc:
            self._failure(runtime, "stop", exc)
        finally:
            runtime.status, runtime.registrations = "disabled", ()

    def registrations(self, companion_id, point=None):
        return tuple(
            sorted(
                (
                    r
                    for (cid, _), runtime in self.runtimes.items()
                    if cid == companion_id and self.is_active(cid, runtime.manifest.id)
                    for r in runtime.registrations
                    if point is None or r.point == point
                ),
                key=lambda r: (r.priority, r.plugin_id, r.key),
            )
        )

    @staticmethod
    def matches(name, patterns):
        return any(name == p or (p.endswith(".*") and name.startswith(p[:-1])) for p in patterns)

    async def emit(self, companion_id, name, payload, source="core"):
        event = BusEvent(name=name, payload=payload, at=self.clock.now(), source=source)
        self.events.append(event)
        self._queue.append((companion_id, event))
        if self._dispatching:
            return
        self._dispatching = True
        try:
            while self._queue:
                cid, emitted = self._queue.popleft()
                for registration in self.registrations(cid, "event_subscriber"):
                    if self.matches(emitted.name, registration.declaration["subscribe"]):
                        await self.invoke(
                            cid,
                            registration.plugin_id,
                            "event_subscriber",
                            registration.key,
                            emitted,
                        )
        finally:
            self._dispatching = False

    async def invoke(
        self, companion_id, plugin_id, point, key, *args, method=None, scene="public_group"
    ):
        runtime = self.runtimes.get((companion_id, plugin_id))
        registration = next(
            (
                r
                for r in self.registrations(companion_id, point)
                if r.plugin_id == plugin_id and r.key == key
            ),
            None,
        )
        if registration is None or registration.implementation is None:
            raise PluginFailure("not_assembled")
        if point == "channel" and method == "events":
            return self._event_feed(runtime, registration)
        task = asyncio.current_task()
        runtime.calls.add(task)
        if point in {"service", "schedule", "event_subscriber"}:
            scene = "public_group"
        try:
            with bind_scene(scene):
                function = (
                    getattr(registration.implementation, method)
                    if method
                    else registration.implementation
                )
                result = function(*args)
                if inspect.isasyncgen(result):
                    result = [item async for item in result]
                elif inspect.isawaitable(result):
                    if point.startswith("behavior.") or point == "context_section":
                        result = await timed(
                            result, self.clock, registration.declaration.get("timeout_s", 2)
                        )
                    else:
                        result = await result
                result = validate_result(point, method, result)
                if point == "embedder" and (
                    len(result) != len(args[0])
                    or any(len(row) != registration.declaration["dimension"] for row in result)
                ):
                    raise TypeError("embedding shape does not match request and manifest")
                return result
        except Exception as exc:
            self._failure(runtime, point, exc)
            if point.startswith("behavior."):
                phase = point.split(".", 1)[1]
                value = thaw(runtime.manifest.data.get("failure_default", {}).get(phase))
                return validate_result(point, None, value)
            if point in {"context_section", "event_subscriber", "schedule"}:
                return None
            if point in {"memory.retriever", "memory.consolidator"}:
                return []
            if point == "model" and method == "quota":
                return "unknown"
            if point == "model" and method == "generate":
                return ModelTurn(finish="error", usage=TokenUsage())
            if (point == "channel" and method in {"send", "react"}) or point == "notifier":
                intent_id = (
                    args[0].intent_id
                    if point == "channel"
                    else f"{runtime.ctx.random.getrandbits(128):032x}"
                )
                return Receipt(intent_id=intent_id, status="unknown", at=self.clock.now())
            raise PluginFailure(type(exc).__name__) from exc
        finally:
            runtime.calls.discard(task)

    async def _event_feed(self, runtime, registration):
        from clytie.sdk.types import EventEnvelope

        task = asyncio.current_task()
        runtime.calls.add(task)
        feed = registration.implementation.events()
        try:
            with bind_scene():
                async for event in feed:
                    if not isinstance(event, EventEnvelope):
                        raise TypeError("channel yielded an invalid event")
                    yield event
        except Exception as exc:
            self._failure(runtime, "channel.events", exc)
        finally:
            if hasattr(feed, "aclose"):
                await feed.aclose()
            runtime.calls.discard(task)

    async def behavior(self, companion_id, phase, input, *, mentioned=False, scene=None):
        point = "behavior." + phase
        event = input[0] if isinstance(input, tuple) else input
        scene = scene or getattr(getattr(event, "conversation", None), "scene", "public_group")
        if phase == "after_generation":
            scene = input.conversation.scene
        opinions = []
        current = list(input) if phase == "before_send" else input
        for reg in self.registrations(companion_id, point):
            supplied = list(current) if phase == "before_send" else current
            result = await self.invoke(
                companion_id, reg.plugin_id, point, reg.key, supplied, scene=scene
            )
            if phase in {"after_generation", "before_send"}:
                check = valid_after_generation if phase == "after_generation" else valid_before_send
                if (
                    result is not None
                    and check(current, result)
                    and (phase != "before_send" or check(current, supplied))
                ):
                    current = result
                elif result is not None or (
                    phase == "before_send" and not check(current, supplied)
                ):
                    self.log.warning(
                        "plugin.invalid_form_change", plugin_id=reg.plugin_id, phase=phase
                    )
            else:
                opinions.append((reg.priority, reg.plugin_id, result))
        conversation = getattr(event, "conversation", None)
        message = getattr(event, "message", None)
        owner_private = (
            conversation is not None
            and conversation.kind == "private"
            and message is not None
            and message.sender.role == "owner"
        )
        if phase == "before_attention":
            return merge_before_attention(opinions, owner_private=owner_private)
        if phase == "attention":
            return merge_attention(
                opinions,
                private=conversation is not None and conversation.kind == "private",
                mentioned=mentioned,
                owner_private=owner_private,
            )
        return current if phase in {"after_generation", "before_send"} else None

    async def context_sections(self, companion_id, request):
        sections = []
        for reg in self.registrations(companion_id, "context_section"):
            result = await self.invoke(
                companion_id, reg.plugin_id, reg.point, reg.key, request, scene=request.scene
            )
            if result is not None:
                sections.append((result, reg.declaration["budget_chars"]))
        return collect_context_sections(sections, request.budget_chars)

    async def execute_tool(self, companion_id, name, call, inv, cancel):
        reg = next((r for r in self.registrations(companion_id, "tool") if r.key == name), None)
        if reg is None:
            return ToolResult(
                call_id=call.call_id, status="denied", content="tool unavailable", data={}
            )
        try:
            cancel.raise_if_cancelled()
            plan = await self.invoke(
                companion_id,
                reg.plugin_id,
                "tool",
                name,
                call,
                inv,
                method="prepare",
                scene=inv.conversation.scene,
            )
            if isinstance(plan, Denial):
                return ToolResult(
                    call_id=call.call_id, status="denied", content=plan.message_for_model, data={}
                )
            result = await self.invoke(
                companion_id,
                reg.plugin_id,
                "tool",
                name,
                plan,
                inv,
                cancel,
                method="execute",
                scene=inv.conversation.scene,
            )
            if result.call_id != call.call_id:
                raise PluginFailure("wrong tool call id")
            return result
        except asyncio.CancelledError:
            if not cancel.cancelled:
                raise
            error = (
                {"code": "deadline", "message": "deadline"} if cancel.reason == "deadline" else None
            )
            return ToolResult(
                call_id=call.call_id, status="cancelled", content="", data={}, error=error
            )
        except Exception:
            return ToolResult(
                call_id=call.call_id, status="error", content="plugin failed", data={}
            )

    def companion_state(self, companion_id):
        return CompanionState(companion_id=companion_id, states=self.states.get(companion_id, {}))

    async def reconfigure(self, companion_id, plugin_id, config=None):
        runtime = self.runtimes[companion_id, plugin_id]
        manifest = runtime.manifest
        try:
            if (companion_id, plugin_id) in self.unit_configs:
                if config is not None:
                    return {"outcome": "rejected", "reason": "validated_report_required"}
                for layout in self.unit_configs[companion_id, plugin_id]:
                    self._config(companion_id, manifest, layout["config"])
                merged = None
            else:
                merged = self._config(companion_id, manifest, config)
        except ValueError as exc:
            return {"outcome": "rejected", "issues": getattr(exc, "issues", ())}
        if not manifest.data.get("hot_reload", False):
            return {"outcome": "restart_required", "reason": "hot_reload_unsupported"}
        await self._stop(companion_id, plugin_id, "reconfigure")
        success = await self._start(companion_id, manifest, override=merged)
        return {
            "outcome": "applied" if success else "rejected",
            "reason": None if success else "start_failed",
        }

    def refresh(self):
        self.manifests = discover(
            self.repo_root / "plugins", self.home / "plugins", repo_root=self.repo_root
        )
        self.manifests = {pid: m for pid, m in self.manifests.items() if pid not in self._excluded}

    def install(self, manifest_path):
        manifest = read_manifest(Path(manifest_path), repo_root=self.repo_root)
        available = discover(
            self.repo_root / "plugins", self.home / "plugins", repo_root=self.repo_root
        )
        if manifest.id in available:
            if manifest.path != available[manifest.id].path:
                raise ValueError("duplicate plugin id; use upgrade for an existing package")
        else:
            destination = contained(self.home, self.home / "plugins" / manifest.id)
            for entry in manifest.path.parent.rglob("*"):
                contained(manifest.path.parent, entry)
            shutil.copytree(manifest.path.parent, destination)
        self._excluded.discard(manifest.id)
        atomic_write(self._excluded_file, canonical(sorted(self._excluded)))
        self.refresh()
        return self.manifests[manifest.id]

    async def dispatch_tick(self, companion_id, plugin_id, schedule_id, tick):
        if tick.schedule_id != schedule_id:
            raise ValueError("tick does not match schedule id")
        return await self.invoke(companion_id, plugin_id, "schedule", schedule_id, tick)

    async def upgrade(self, companion_id, plugin_id, manifest_path, cancel=None):
        cancel = cancel or CancelToken()
        async with self._lock:
            cancel.raise_if_cancelled()
            old = self.runtimes[companion_id, plugin_id]
            if old.status != "active":
                raise ValueError("plugin must be active before explicit upgrade")
            candidate = read_manifest(Path(manifest_path), repo_root=self.repo_root)
            if candidate.id != plugin_id:
                raise ValueError("upgrade plugin id mismatch")
            if candidate.path.parent == old.manifest.path.parent:
                raise ValueError("stage the upgrade in a separate package directory")
            old_data = old.manifest.data.get("data", {}).get("version", 1)
            new_data = candidate.data.get("data", {}).get("version", 1)
            if new_data < old_data or candidate.version < old.manifest.version:
                raise ValueError("data_downgrade: refusing an upgrade to an older version")
            scripts = []
            for version in range(old_data + 1, new_data + 1):
                path = contained(
                    candidate.path.parent, candidate.path.parent / "migrations" / f"to_{version}.py"
                )
                if not path.is_file():
                    raise ValueError(f"missing migration to_{version}.py")
                scripts.append(path)
            for layout in self.unit_configs.get((companion_id, plugin_id), [{"config": None}]):
                self._config(companion_id, candidate, layout["config"])
            proposed = dict(self.manifests)
            proposed[plugin_id] = candidate
            resolve(proposed, self._active(companion_id))
            old_class, old_config = type(old.plugin), old.ctx.config
            previous_layouts = [
                {"role": u.unit_role, "account": u.account_ref, "config": u.ctx.config}
                for u in old.units or [old]
            ]
            old.status = "quiescing"
            try:
                if old.calls:
                    await timed(
                        asyncio.gather(*tuple(old.calls), return_exceptions=True), self.clock, 10
                    )
                snapshot = self.snapshots.create(
                    companion_id, plugin_id, str(old.manifest.version), str(candidate.version)
                )
            except BaseException:
                old.status = "active"
                raise
            await self._stop(companion_id, plugin_id, "upgrading")
            migration = Runtime(companion_id, candidate, old.storage, old.priority)
            previous_source = None
            installed_source = old.manifest.path.parent
            try:
                migration.ctx = build_context(
                    self, migration, self._config(companion_id, candidate), {}, permissions=()
                )
                for script in scripts:
                    cancel.raise_if_cancelled()
                    module = ModuleType("_clytie_migration")
                    exec(compile(script.read_bytes(), str(script), "exec"), module.__dict__)
                    function = getattr(module, "migrate")
                    if not inspect.iscoroutinefunction(function):
                        raise TypeError("migration must be asynchronous")
                    with bind_scene():
                        await function(migration.ctx, cancel)
                    cancel.raise_if_cancelled()
                if candidate.path.parent != installed_source:
                    expected = [
                        self.repo_root / "plugins" / plugin_id,
                        self.home / "plugins" / plugin_id,
                    ]
                    if installed_source not in expected:
                        raise ValueError("installed package must remain in a plugin root")
                    for entry in candidate.path.parent.rglob("*"):
                        contained(candidate.path.parent, entry)
                    staging = contained(
                        self.home, self.home / "plugins" / ".host" / f"upgrade-{self._serial}"
                    )
                    staging.mkdir(parents=True, exist_ok=False)
                    copied = staging / "new"
                    shutil.copytree(candidate.path.parent, copied)
                    # 两个移动目标均先校验在已知插件根/本次 staging 内。
                    permitted = (
                        self.repo_root
                        if installed_source.is_relative_to(self.repo_root)
                        else self.home
                    )
                    contained(permitted, installed_source)
                    previous_source = contained(staging, staging / "previous")
                    installed_source.rename(previous_source)
                    try:
                        contained(staging, copied).rename(installed_source)
                    except BaseException:
                        previous_source.rename(installed_source)
                        previous_source = None
                        raise
                    candidate = read_manifest(
                        installed_source / "plugin.json", repo_root=self.repo_root
                    )
                if not await self._start(companion_id, candidate, version_check=False):
                    raise PluginFailure("upgraded plugin start failed")
                self._record_version(companion_id, candidate)
                self.manifests[plugin_id] = candidate
                return {"outcome": "upgraded", "snapshot": str(snapshot)}
            except (Exception, asyncio.CancelledError) as exc:
                await self._stop(companion_id, plugin_id, "upgrade_failed")
                if previous_source is not None:
                    failed_source = contained(
                        previous_source.parent, previous_source.parent / "failed"
                    )
                    contained(
                        self.repo_root
                        if installed_source.is_relative_to(self.repo_root)
                        else self.home,
                        installed_source,
                    )
                    installed_source.rename(failed_source)
                    contained(previous_source.parent, previous_source).rename(installed_source)
                self.snapshots.restore(snapshot, companion_id, plugin_id)
                self._record_version(companion_id, old.manifest)
                self.manifests[plugin_id] = old.manifest
                self.unit_configs[companion_id, plugin_id] = previous_layouts
                restored = await self._start(
                    companion_id, old.manifest, cls=old_class, override=old_config
                )
                self._failure(old, "migration", exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return {
                    "outcome": "restored" if restored else "restore_start_failed",
                    "snapshot": str(snapshot),
                }

    async def close(self):
        for cid in sorted({key[0] for key in self.runtimes}):
            active = self._active(cid)
            for pid in reversed(resolve(self.manifests, active).order) if active else ():
                await self._stop(cid, pid, "shutdown")
