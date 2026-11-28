"""入站编排、模型工具循环、主动话题与共用出站流水线。"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from uuid import uuid4

from jsonschema import Draft202012Validator

from clytie.backup.coordination import run_sync
from clytie.config import thaw
from clytie.kernel.delivery import OwnedFinalization
from clytie.kernel.events import background
from clytie.kernel.ledger import EXEC_TERMINAL
from clytie.kernel.service import record_scope
from clytie.kernel.task_card import render_task_card
from clytie.plugin_host.memory import bind_scene
from clytie.sdk import CancelToken, ProactiveSubmission
from clytie.sdk.behavior import Defer, Ignore, Judge, Judgment, Mode, ReactOnly, Respond, Skip
from clytie.sdk.channel import channel_state
from clytie.sdk.context import validate_proactive
from clytie.sdk.types import (
    Budget,
    ChatMessage,
    Conversation,
    DeliveryIntent,
    Denial,
    Invocation,
    ModelRequest,
    ReplyDraft,
    TextPart,
    ToolResult,
    ToolSpec,
)
from clytie.storage.owner import StorageBusy, StorageCancelled, StorageFailed

from .addressed import ConversationMessages
from .appearance import VIEW, Appearance, load_appearance, reference_message, self_check
from .context import Context, message
from .image_context import ImageContext, view_tool
from .judgment import AttentionJudgments
from .materials import NOTICE, RECALL, Materials
from .persona import Persona
from .plugin_models import PluginModels
from .policy import CATEGORIES, Boundary, Outbound, Protection, fast_cancel, text_parts
from .prewrite import Prewrites
from .proactive_pause import ProactivePause
from .safety_belt import SafetyBelt
from .vision import ImageFetcher

REASONS = {
    "proactive_limit": "今天主动用这个能力的次数用完了",
    "permission": "对方没有使用这个能力的权限",
    "protection_mode": "额度紧张，暂停非主人的重任务",
    "disabled": "这个能力已停用",
    "start_failed": "这个能力暂时维护中",
    "migrating": "这个能力暂时维护中",
    "model_no_tools": "当前模型不支持使用工具",
}


def invalid_argument_location(schema, error):
    """定位嵌套列表项，只使用清单声明的字段名和序号，不回显参数。"""
    path = ""
    for key in error.absolute_path:
        if not isinstance(schema, dict):
            break
        if isinstance(key, int):
            path += f"[{key + 1}]"
            schema = schema.get("items")
        elif key in schema.get("properties", {}):
            path += ("." if path else "") + key
            schema = schema["properties"][key]
        else:
            break
    if "[" in path:
        return f"invalid_arguments：{path}（序号从 1 开始），未满足 {error.validator}。"
    return "invalid_arguments"


def asset_part_groups(assets, features):
    """按一个工具结果的产物顺序装消息，只有相邻的图片可以合并。"""
    images = []
    size = 0
    image_limit = features.media_limits.get("image")
    for asset in assets:
        media = asset.media_type.partition("/")[0]
        kind = media if media in {"image", "audio", "video"} else "file"
        part = {"type": kind, "asset": asset}
        if images and (
            kind != "image"
            or len(images) >= features.max_images_per_message
            or size + asset.size > image_limit["max_bytes"]
        ):
            yield images
            images = []
            size = 0
        if kind == "image" and image_limit:
            images.append(part)
            size += asset.size
        else:
            yield [part]
    if images:
        yield images


class Capability:
    def __init__(self, host, cid, reg, material_budget=6000):
        self.host, self.cid, self.reg = host, cid, reg
        self.material_budget = material_budget

    def idempotency_key(self, call, inv):
        with bind_scene(inv.conversation.scene, inv.conversation.conversation_id):
            return self.reg.implementation.idempotency_key(call, inv)

    async def prepare(self, call, inv):
        return await self.host.invoke(
            self.cid,
            self.reg.plugin_id,
            "tool",
            self.reg.key,
            call,
            inv,
            method="prepare",
            scene=inv.conversation.scene,
            conversation_id=inv.conversation.conversation_id,
        )

    async def execute(self, plan, inv, cancel):
        result = await self.host.invoke(
            self.cid,
            self.reg.plugin_id,
            "tool",
            self.reg.key,
            plan,
            inv,
            cancel,
            method="execute",
            scene=inv.conversation.scene,
            conversation_id=inv.conversation.conversation_id,
        )
        material = result.material
        if material and material.url and len(material.text) > self.material_budget:
            marker = "[已截断]"
            text = material.text[: max(0, self.material_budget - len(marker))] + marker
            result = result.model_copy(
                update={
                    "material": material.model_copy(update={"text": text}),
                    "content": text,
                }
            )
        return result


class Agent:
    def __init__(
        self,
        kernel,
        host,
        config,
        instance,
        persona_root,
        *,
        model=None,
        notifier=None,
        memory_context=None,
    ):
        self.kernel, self.host, self.config, self.instance = kernel, host, config, instance
        from clytie.admission import Admission

        self.admission = Admission(
            config, instance, channels=kernel.delivery.channels, clock=kernel.clock
        )
        kernel.delivery.admission = self.admission
        self.ledger, self.clock, self.cid = kernel.ledger, kernel.clock, kernel.ledger.companion_id
        self.log = kernel.log
        self.proactive_pause = ProactivePause(host.home, self.cid, self.clock)
        self.conversation_messages = ConversationMessages(self.ledger, config.get("names", {}))
        self.judgments = AttentionJudgments(self)
        self.safety_belt = SafetyBelt(self, notifier=notifier)
        self.model_registration = (
            None
            if model is not None
            else next(
                r
                for r in host.registrations(self.cid, "model")
                if r.plugin_id == config["model"]["plugin"] and r.key == "model"
            )
        )
        self.model = model or self.model_registration.implementation
        self.features = self.model.features()
        self.persona = Persona(persona_root, config)
        self.appearance = Appearance()
        self.appearance_warnings = []
        self.appearance_summary = None
        self.appearance_check_task = None
        self.actor_names = {}
        self.boundary = Boundary(
            persona_root.parent / "boundary/state.json",
            config.get("boundary", {}),
            self.clock,
            self.ledger,
            self.log,
            notifier,
        )
        self.outbound = Outbound(config)
        self.protection = Protection(
            config.get("protection", {}), kernel.emit, config["model"]["plugin"]
        )
        self.context = Context(self)
        self.image_fetcher = ImageFetcher(
            self.clock, config.get("vision", {}).get("max_image_bytes", 20 * 1024 * 1024)
        )
        self.media_jobs = {}
        self.image_publication = asyncio.Lock()
        from clytie.memory.context import MemoryContext

        self.memory = MemoryContext(
            host.memory_store(self.cid, config, instance),
            log=self.log,
            ledger=self.ledger,
            changed=self.rebuild_memory,
        )
        self.memory.store.changed = self.rebuild_memory
        self.outbound.memory_guard = self.memory.guard
        self.memory_context = memory_context
        self.batches, self.background, self.warnings = {}, set(), []
        self.intake_tasks = set()
        self.filtered_count = 0
        self.model_health = "unknown"
        self.model_error_code = None
        self.shutting_down = False
        self.prewrites = Prewrites(self)
        self.opening = {}
        self.proactive_writing = {}
        self.plugin_models = PluginModels(self)
        kernel.config = config
        kernel.permissions = lambda actor, tool: (
            self.reason(actor, tool)
            not in {"permission", "disabled", "start_failed", "migrating", "model_no_tools"}
        )
        kernel.quota = lambda actor, tool: (
            not self.protection.active
            or (actor is not None and actor.role == "owner")
            or not self.declarations().get(tool, (None, {}))[1].get("heavy")
        )
        kernel.conversation_scenes.update(
            {key: value["scene"] for key, value in instance.get("conversations", {}).items()}
        )

    async def model_call(
        self, method, *args, scene="public_group", conversation_id=None, update_health=True
    ):
        from clytie.sdk.types import ModelTurn

        if method == "generate" and self.model_health == "auth_invalid":
            return ModelTurn(
                finish="error",
                usage={},
                error={
                    "code": "auth_invalid",
                    "message": "",
                },
            )
        from clytie.assets.store import model_image_access

        selected = (
            [p.asset for m in args[0].messages for p in m.parts if p.type == "image" and p.asset]
            if method == "generate"
            else []
        )
        with model_image_access(
            self.cid,
            self.config["model"]["plugin"],
            selected,
            appearance={r.asset.asset_id: r.resolved for r in self.appearance.references},
        ):
            if self.model_registration is None:
                result = await getattr(self.model, method)(*args)
            else:
                reg = self.model_registration
                result = await self.host.invoke(
                    self.cid,
                    reg.plugin_id,
                    "model",
                    reg.key,
                    *args,
                    method=method,
                    scene=scene,
                    conversation_id=conversation_id,
                )
        if method == "generate" and update_health:
            self.observe_model_health(result)
        return result

    def observe_model_health(self, turn):
        self.model_error_code = turn.error.code if turn.error else None
        if self.model_error_code == "auth_invalid":
            self.model_health = "auth_invalid"
        elif self.model_health != "auth_invalid":
            self.model_health = "ok"

    def rebuild_memory(self):
        if getattr(self, "shutting_down", False):
            return
        self._memory_dirty = True
        if not getattr(self, "_memory_rebuilding", None) or self._memory_rebuilding.done():

            async def rebuild():
                while self._memory_dirty:
                    self._memory_dirty = False
                    await self.memory.retrieval.rebuild()

            self._memory_rebuilding = self.track(rebuild())
            jobs = self.host.memory_jobs.setdefault(self.cid, set())
            jobs.add(self._memory_rebuilding)
            self._memory_rebuilding.add_done_callback(jobs.discard)

    async def start(self):
        from clytie.appearance import AppearanceFiles

        if self.features.agent_type:
            raise ValueError("unsupported_model_type")
        files = AppearanceFiles(self.host.home, self.cid, self.clock)
        self.appearance, self.appearance_warnings = await asyncio.to_thread(load_appearance, files)
        await self.boundary.initialize()
        self.protection.update(await self.model_call("quota"))
        self.host.plugin_models[self.cid] = self.plugin_models
        self.host.service_agents[self.cid] = self
        await self.kernel.start()
        from clytie.memory.retrieval import HostExtension

        for name, point in (("embedder", "embedder"), ("reranker", "memory.reranker")):
            pid = self.config.get("memory", {}).get(name)
            reg = next(
                (r for r in self.host.registrations(self.cid, point) if r.plugin_id == pid), None
            )
            if reg:
                setattr(self.memory.retrieval, name, HostExtension(self.host, self.cid, reg))
                if name == "embedder":
                    self.memory.retrieval.embedder_id = pid
        self.rebuild_memory()
        from clytie.memory.scheduler import Recollector

        self.recollector = Recollector(self)
        await self.recollector.start()
        if self.appearance.description or self.appearance.references or self.appearance_warnings:
            if self.config.get("appearance", {}).get("self_check", True):
                self.appearance_check_task = self.track(self_check(self, files))
            else:
                self.appearance_summary = "跳过"

    def track(self, coroutine):
        task = background(coroutine)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    async def _fetch_image(self, channel, part, key, index):
        status, image = await self.image_fetcher.fetch(channel, part)
        asset = None
        if image is not None:
            try:
                async with self.image_publication:
                    asset = await self.host.asset_store.put(
                        self.cid, image.data, image.media_type, "inbound_media", inbound_only=True
                    )
                    asset = asset.model_copy(
                        update={"origin": "inbound_media", "media_type": image.media_type}
                    )
            except Exception:
                status = "failed"
        await run_sync(
            self.ledger,
            self.ledger.update_media,
            key,
            index,
            status,
            asset,
            first_frame=bool(asset and image.first_frame),
        )

    def declarations(self):
        result = {}
        selected = (
            set(self.config.get("plugins", {}))
            | {self.config["model"]["plugin"]}
            | {c["plugin"] for c in self.config.get("channels", [])}
        )
        for pid, manifest in self.host.manifests.items():
            if pid in selected:
                for declaration in manifest.data["contributes"].get("tools", []):
                    result[declaration["name"]] = (pid, declaration)
        return result

    def reason(self, actor, tool, conversation=None):
        item = self.declarations().get(tool)
        if not item:
            return "disabled"
        pid, declaration = item
        if not self.features.supports_tools:
            return "model_no_tools"
        allowed = self.config.get("permissions", {}).get(actor.role, []) if actor else []
        if (
            actor is None
            or actor.role not in declaration["audience"]
            or tool not in allowed
            and not (actor.role == "owner" and "*" in allowed)
        ):
            return "permission"
        if conversation is not None and not self.scope_allows(actor, tool, conversation):
            return "permission"
        reg = next((r for r in self.host.registrations(self.cid, "tool") if r.key == tool), None)
        if reg is None:
            state = self.host.states.get(self.cid, {}).get(pid, {}).get("status", "disabled")
            return state if state in {"start_failed", "migrating"} else "disabled"
        if actor.role != "owner" and declaration.get("heavy") and self.protection.active:
            return "protection_mode"
        return None

    def scope_allows(self, actor, tool, conversation):
        scope = self.config.get("tool_scopes", {}).get(tool)
        if scope is None:
            return True
        if conversation.kind not in {"private", "group"} or actor is None:
            return False
        if actor.role != "owner" and actor.actor_id in scope.get("exclude", ()):
            return False
        roles = scope.get(conversation.kind)
        return roles is None or actor.role in roles

    def service_allowed(self, call, tool):
        actor = call.actor
        if actor is None:
            actor = self.admission.private_actors.get(call.conversation.conversation_id)
            if call.conversation.kind != "private" or actor is None or actor.role != "owner":
                return False
            declaration = self.declarations().get(tool)
            if declaration is None or (self.protection.active and declaration[1].get("heavy")):
                return False
        return self.reason(actor, tool, call.conversation) is None

    async def audit_service(self, trace, pid, service, method, status, products, usage):
        def write():
            with self.ledger.transaction():
                self.ledger.audit(
                    "service.call",
                    trace_id=trace,
                    plugin=pid,
                    service=service,
                    method=method,
                    status=status,
                    asset_ids=products,
                    usage=usage,
                )

        await self.kernel.persist(trace, write)

    async def cancellable(self, conversation, actor=None):
        return [
            r
            for r in await run_sync(self.ledger, self.ledger.rows, "record")
            if r["conversation_id"] == conversation.conversation_id
            and r["kind"] == "task"
            and (r["exec_status"] not in EXEC_TERMINAL or r["delivery_status"] == "pending")
            and (
                actor is None
                or actor.role == "owner"
                or r["requester"]
                and r["requester"]["actor_id"] == actor.actor_id
            )
        ]

    async def tools(self, conversation, actor, proactive=False, *, proactive_tools=False):
        if proactive and not proactive_tools:
            return [], "主动开口时暂不使用能力"
        limits = self.config.get("proactive", {}).get("tools", {})
        counts = {}
        if proactive:
            actor = self.admission.private_actors.get(conversation.conversation_id)
            if actor is None or actor.role != "owner":
                return [], "主动能力只可用于已登记的主人私聊"
            counts = await run_sync(self.ledger, self.ledger.proactive_counts)
        specs, lines = [], []
        for name, (_, declaration) in self.declarations().items():
            if proactive and name not in limits:
                continue
            reason = self.reason(actor, name, conversation)
            if proactive and not reason:
                if declaration.get("heavy") and self.protection.active:
                    reason = "protection_mode"
                elif counts.get(name, 0) >= limits[name]["daily"]:
                    reason = "proactive_limit"
            detail = REASONS[reason] if reason else declaration["description"]
            if reason == "protection_mode" and self.protection.snapshot:
                resets = [
                    w.resets_at.isoformat() for w in self.protection.snapshot.windows if w.resets_at
                ]
                detail += "；预计恢复：" + ", ".join(resets)
            lines.append(f"{name}: {reason or 'available'} {detail}")
            if not reason:
                specs.append(
                    ToolSpec.model_validate(
                        {
                            k: thaw(declaration[k])
                            for k in (
                                "name",
                                "description",
                                "when_not_to_use",
                                "examples",
                                "parameters",
                            )
                        }
                    )
                )
        if not self.features.supports_tools:
            return [], "\n".join(lines)
        specs.extend(self.memory.specifications(None if proactive else actor))
        if not proactive and await self.cancellable(conversation):
            specs.append(
                ToolSpec(
                    name="core_cancel_task",
                    description="取消最近任务中明确指定的任务；目标不明确或有多个可能时先反问，不要猜。",
                    when_not_to_use="没有明确目标",
                    parameters={
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string", "pattern": "^[0-9a-fA-F]{8}$"}
                        },
                        "required": ["task_id"],
                        "additionalProperties": False,
                    },
                )
            )
        categories = [
            c
            for c in CATEGORIES
            if self.config.get("boundary", {}).get("categories", {}).get(c, True)
        ]
        if not proactive and actor.role not in {"owner", "peer"} and categories:
            specs.append(
                ToolSpec(
                    name="core_report_boundary",
                    description="登记恋人关系、修改人设、指定亲密台词、亲密或暗示性图像、主人隐私类越界请求；主人隐私类明确拒绝后仍反复追问、转弯打听也计入。正常玩笑、好奇、转述和讨论不计入。",
                    when_not_to_use="玩笑、好奇、转述或讨论",
                    parameters={
                        "type": "object",
                        "properties": {"category": {"enum": categories}},
                        "required": ["category"],
                        "additionalProperties": False,
                    },
                )
            )
        for spec in specs:
            if not self.scope_allows(actor, spec.name, conversation) and spec.name.startswith(
                "core_"
            ):
                lines.append(f"{spec.name}: permission {REASONS['permission']}")
        return [s for s in specs if self.scope_allows(actor, s.name, conversation)], "\n".join(
            lines
        )

    async def receive(self, event, *, holding=None, historical=False):
        if event.companion_id != self.cid:
            raise ValueError("wrong companion")
        if not await self.admission.accept_checked(event):
            return None
        if event.addressed is not None:
            event = event.model_copy(update={"addressed": None})
        images = {}
        if event.message:
            parts = list(event.message.parts)
            for i, part in enumerate(parts):
                if part.type == "image":
                    images[i] = part
                    parts[i] = part.model_copy(update={"asset": None, "raw_ref": None})
            if images:
                event = event.model_copy(
                    update={"message": event.message.model_copy(update={"parts": parts})}
                )
        finalization = (
            holding.reserve(self.kernel.delivery)
            if holding is not None
            else OwnedFinalization(self.kernel.delivery)
        )
        state = {"accepted": False, "handed": False, "images": images}
        state.update(holding=holding, historical=historical)

        async def receive_owned():
            try:
                return await self._receive(event, finalization, state)
            except BaseException as exc:
                if state["accepted"] and not state["handed"]:
                    if state.get("trace"):
                        await self.kernel.seal(
                            state["trace"],
                            "cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                            error={"code": getattr(exc, "code", "agent_error"), "message": ""},
                        )
                    await finalization.persist(
                        self._ignore_unhandled, [event.event_id], getattr(exc, "code", None)
                    )
                raise
            finally:
                if not state["handed"]:
                    finalization.release()

        # 断线不遗弃已进入接受流程的本体任务；它仍由责任对象跟踪。
        return await self._intake(receive_owned(), finalization)

    async def _intake(self, coroutine, finalization):
        task = finalization.track(asyncio.create_task(coroutine))
        self.intake_tasks.add(task)
        task.add_done_callback(self.intake_tasks.discard)
        return await asyncio.shield(task)

    async def _claim_inbound(self, event, finalization, state):
        # 接受点仍使用普通容量，满时明确抛出 storage_busy，不能借收尾槽假接收。
        command = finalization.track(
            asyncio.create_task(
                run_sync(
                    self.ledger, self.ledger.claim_inbound, event, historical=state["historical"]
                )
            )
        )
        key, new = await asyncio.shield(command)
        state["accepted"] = new
        if new and state.get("historical") and state.get("holding") is not None:
            state["holding"].unseen += 1
        if new and event.message and event.message.sender.role == "owner":
            trace = self.proactive_writing.get(event.conversation.conversation_id)
            if trace is not None:
                record = await finalization.persist(self.ledger.get, "record", trace)
                if record and record.get("proactive_result") == "superseded":
                    self.kernel.tokens.setdefault(trace, CancelToken()).cancel("superseded")
        return key, new

    def _ignore_unhandled(self, keys, reason=None):
        for key in keys:
            row = self.ledger.get("inbound", key)
            if row and row["state"] == "received":
                self.ledger.ignore(key, reason)

    def _bind_finalization(self, trace, finalization):
        self.kernel.finalizations[trace] = finalization

        def forget():
            if self.kernel.finalizations.get(trace) is finalization:
                self.kernel.finalizations.pop(trace, None)

        finalization.on_complete(forget)

    async def _settle_record(self, trace, finalization, future):
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            await self.kernel.seal(trace, "cancelled", error={"code": "cancelled", "message": ""})
        except Exception as exc:
            await self.kernel.seal(
                trace, "error", error={"code": getattr(exc, "code", "agent_error"), "message": ""}
            )
            raise
        finally:
            finalization.release()

    async def _receive(self, event, finalization, state):
        if event.companion_id == self.cid and event.message:
            actor = event.message.sender
            self.actor_names[actor.actor_id] = actor.display_name
        reason = (
            "silenced"
            if self.kernel.delivery.silenced
            else "shutting_down"
            if self.shutting_down
            else "model_auth_invalid"
            if self.model_health == "auth_invalid"
            else None
        )
        if reason:
            if event.companion_id != self.cid:
                raise ValueError("wrong companion")
            if event.dedupable:
                key, new = await self._claim_inbound(event, finalization, state)
                if new:
                    await finalization.persist(self.ledger.ignore, key, reason)
            return None
        if event.companion_id != self.cid:
            raise ValueError("wrong companion")
        self.kernel.conversation_scenes[event.conversation.conversation_id] = (
            event.conversation.scene
        )
        if self.boundary.filtered(event):
            self.filtered_count += 1
            self.log.debug("inbound.filtered")
            return None
        if event.message and self.boundary.paused(event.message.sender):
            if event.dedupable:
                key, new = await self._claim_inbound(event, finalization, state)
                if new:
                    await finalization.persist(self.ledger.ignore, key, "boundary_paused")
            return None
        if not event.dedupable:
            event = await self._mark_addressed(event, finalization)
            await self.host.behavior(self.cid, "message_received", event)
            return None
        key, new = await self._claim_inbound(event, finalization, state)
        if not new:
            self.kernel.duplicates += 1
            return None
        event = await self._mark_addressed(event, finalization)
        holding = state["holding"]
        if holding is not None and holding.stage(event, finalization, state):
            state["handed"] = True
            return
        return await self._process_claimed(event, finalization, state)

    async def _process_claimed(
        self, event, finalization, state, *, batch_note=None, assemble=False
    ):
        key = event.event_id
        if (
            event.message
            and event.conversation.kind == "group"
            and event.message.sender.role != "owner"
            and await finalization.read(self.safety_belt.paused, event.conversation.conversation_id)
        ):
            if not state.get("retry"):
                await finalization.persist(self.ledger.ignore, key, "safety_paused")
            await self.host.behavior(self.cid, "message_received", event)
            return None
        channel = self.kernel.delivery.channels.get(event.channel_id)
        for index, part in state.get("images", {}).items():
            if channel is not None and channel.features().supports_media_fetch:
                await finalization.persist(self.ledger.update_media, key, index, "pending")
                job = self.track(self._fetch_image(channel, part, key, index))
                media_key = (key, index)
                self.media_jobs[media_key] = job
                job.add_done_callback(lambda _, k=media_key: self.media_jobs.pop(k, None))
        if hasattr(self, "recollector"):
            self.recollector.activity(event.conversation)
        if not event.message:
            await self.host.behavior(self.cid, "message_received", event)
            await finalization.persist(self.ledger.ignore, key)
            return None
        mentioned = any(p.type == "mention" and p.is_self for p in event.message.parts)
        cancel = None if state["historical"] else fast_cancel(event.message, self.cancel_words())
        if (
            event.conversation.kind == "group"
            and not mentioned
            and cancel is not None
            and await self.cancellable(event.conversation, event.message.sender)
        ):
            await self.cancel(event, cancel)
            await finalization.persist(self.ledger.ignore, key, "cancel_only")
            return None
        await self.host.behavior(self.cid, "message_received", event)
        before = await self.host.behavior(
            self.cid, "before_attention", (event, self.host.companion_state(self.cid))
        )
        if isinstance(before, Ignore):
            if not state.get("retry"):
                await finalization.persist(self.ledger.ignore, key)
            return None
        if isinstance(before, ReactOnly) and state.get("batch_reaction"):
            state["reaction"] = (before.emoji, event.message.ref)
        elif isinstance(before, ReactOnly):
            record = await self.kernel.open_record(event_id=key, finalization=finalization)
            state["trace"] = record["trace_id"]
            self._bind_finalization(record["trace_id"], finalization)
            await self._react_only(record, finalization, before.emoji, event.message.ref)
            return record["trace_id"]
        batch_key = (event.conversation.conversation_id, event.message.sender.actor_id)
        if assemble:
            batch_key += (state["holding"].batch_id,)
        if batch_key in self.batches:
            batch = self.batches[batch_key]
            batch["events"].append(event)
            batch["finalizations"].append(finalization)
            state["handed"] = True
            if assemble:
                return batch_key
            if batch.get("prepare_allowed"):
                batch["generation"] += 1
                generation = batch["generation"]
                previous = batch.pop("timer", None)
                if previous is not None:
                    previous.cancel()
                await self.prewrites.discard(batch, "batch_changed")
                if generation == batch["generation"]:
                    return await self.attention(batch_key)
            return None
        batch = {
            "generation": 0,
            "events": [event],
            "finalizations": [finalization],
            "started": self.clock.monotonic(),
            "mode": before if isinstance(before, Mode) else None,
            "catch_up": batch_note,
        }
        self.batches[batch_key] = batch
        state["handed"] = True
        if assemble:
            return batch_key
        return await self.attention(batch_key)

    async def _mark_addressed(self, event, finalization):
        return await finalization.read(self.conversation_messages.mark, event)

    async def _react_only(self, record, finalization, emoji, target):
        trace = record["trace_id"]
        intent = self.intent(record, [], kind="react", emoji=emoji, target=target)
        # 宿主已检验插件 message.react；owner Ignore 降级是本体例外。
        await self.kernel.allow_reaction_exception(trace, intent, permissions=("message.react",))
        await self.kernel.seal(trace, "ok", [intent])
        await self.kernel.delivery.deliver(trace, finalization=finalization)

    async def attention(self, key, *, judgment=None, registration=None):
        batch = self.batches[key]
        generation = batch["generation"]
        try:
            return await self._attention(key, batch, judgment, registration)
        except BaseException as exc:
            if batch["generation"] != generation:
                raise
            if not batch.get("queued"):
                await self.prewrites.discard(batch, "stopped" if self.shutting_down else "error")
                if self.batches.get(key) is batch:
                    self.batches.pop(key)
                finalization = batch["finalizations"][0]
                if batch.get("trace"):
                    await self.kernel.seal(
                        batch["trace"],
                        "cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                        error={"code": getattr(exc, "code", "agent_error"), "message": ""},
                    )
                await finalization.persist(
                    self._ignore_unhandled, [e.event_id for e in batch["events"]]
                )
                for responsibility in batch["finalizations"]:
                    responsibility.release()
            raise

    async def _attention(self, key, batch, judgment, registration):
        finalization = batch["finalizations"][0]
        event = batch["events"][-1]
        generation = batch["generation"]

        def paused():
            return (
                event.conversation.kind == "group"
                and event.message.sender.role != "owner"
                and self.safety_belt.paused(event.conversation.conversation_id)
            )

        recovery = batch.get("recovery")
        if recovery and not await recovery.should_respond(batch):
            self.batches.pop(key)
            for responsibility in batch["finalizations"]:
                responsibility.release()
            return None
        if batch.get("reaction"):
            self.batches.pop(key)
            async with self._opening_record(event.conversation.conversation_id):
                return await self._respond(batch, Respond(), paused, False)
        judgment = judgment or batch.get("stale_judgment")
        supplied = (event, [e.message for e in batch["events"]])
        origin = []
        if judgment is not None:
            supplied += (judgment,)
        opinion = await self.host.behavior(
            self.cid,
            "attention",
            supplied,
            judge_origin=origin,
            resume_registration=registration,
            mentioned=any(e.addressed.mention or e.addressed.reply_to_self for e in batch["events"])
            or bool(batch.get("stale_judgment")),
        )

        if generation != batch["generation"]:
            return None

        remaining = 120 - (self.clock.monotonic() - batch["started"])
        defer = isinstance(opinion, Judge) or (isinstance(opinion, Defer) and remaining > 0)
        already_paused = defer and await finalization.read(paused)
        if generation != batch["generation"]:
            return None
        if defer and not already_paused and not self.shutting_down:
            if opinion.prepare and event is not batch["events"][-1]:
                return await self.attention(key)
            batch["prepare_allowed"] = opinion.prepare
            if opinion.prepare:
                await self.prewrites.start(batch)
            else:
                await self.prewrites.discard(batch, "skipped")
            if generation != batch["generation"]:
                return None
            deadline = self.clock.now() + timedelta(seconds=max(0, remaining))

            async def later():
                if isinstance(opinion, Judge):
                    result = Judgment(status="failed")
                    if batch.get("stale_judgment") is not None:
                        result = batch["stale_judgment"]
                    elif remaining > 0:
                        result = await self.judgments.decide(
                            event.conversation,
                            tuple(batch["events"]),
                            opinion.facts,
                            deadline=deadline,
                            finalization=finalization,
                        )
                    if batch["generation"] == generation:
                        await self.attention(key, judgment=result, registration=origin[0])
                else:
                    await self.clock.sleep(min(opinion.seconds, remaining))
                    if batch["generation"] == generation:
                        await self.attention(key)

            async def deferred():
                try:
                    await later()
                except BaseException:
                    if self.batches.get(key) is batch and generation == batch["generation"]:
                        self.batches.pop(key, None)
                        await self.prewrites.discard(
                            batch, "stopped" if self.shutting_down else "error"
                        )
                        await finalization.persist(
                            self._ignore_unhandled, [e.event_id for e in batch["events"]]
                        )
                        for responsibility in batch["finalizations"]:
                            responsibility.release()
                    raise

            batch["timer"] = finalization.track(self.track(deferred()))
            return None
        self.batches.pop(key)
        # Reserve the order of batches which have already reached their final opinion.
        cid = event.conversation.conversation_id
        async with self._opening_record(cid):
            return await self._respond(batch, opinion, paused, already_paused)

    @asynccontextmanager
    async def _opening_record(self, cid):
        predecessor = self.opening.get(cid)
        submitted = asyncio.get_running_loop().create_future()
        self.opening[cid] = submitted
        try:
            if predecessor:
                await asyncio.shield(predecessor)
            yield
        finally:
            submitted.set_result(None)
            if self.opening.get(cid) is submitted:
                del self.opening[cid]

    async def _respond(self, batch, opinion, paused, already_paused):
        event = batch["events"][-1]
        finalization = batch["finalizations"][0]
        reason = None
        sources = batch.get("retry_sources", {})

        def should_open():
            nonlocal reason
            safety_paused = already_paused or paused()
            recovery_reason = (
                batch["recovery"].guard_reason(event) if batch.get("recovery") else None
            )
            if safety_paused or isinstance(opinion, Skip) or self.shutting_down or recovery_reason:
                reason = (
                    "stopped"
                    if self.shutting_down
                    else "safety_paused"
                    if safety_paused
                    else recovery_reason or "skipped"
                )
                if reason == "skipped" and sources and event.conversation.kind == "group":
                    self.ledger.skip_catch_up(
                        [e.event_id for e in batch["events"] if e.event_id not in sources],
                        list(dict.fromkeys(sources.values())),
                    )
                else:
                    self._ignore_unhandled(
                        [e.event_id for e in batch["events"]],
                        recovery_reason or ("safety_paused" if safety_paused else None),
                    )
                return False
            return True

        prepared_round = None
        draft = batch.get("draft")
        if draft and not (isinstance(opinion, Skip) or already_paused or self.shutting_down):
            # Avoid waiting for an obsolete model request. Recheck both guards when
            # opening the record, since another batch may finish during the wait.
            def invalidated():
                if paused():
                    return "safety_paused"
                if self.ledger.record_opened_since(
                    event.conversation.conversation_id, draft.watermark
                ):
                    return "stale"
                return None

            invalid = await finalization.read(invalidated)
            if invalid:
                already_paused = invalid == "safety_paused"
                await self.prewrites.discard(batch, invalid)
            elif batch.get("draft") is draft:
                prepared_round = await self.prewrites.result(batch)
        batch["opening_record"] = True
        record = await self.kernel.open_record(
            event_id=event.event_id,
            retry_of_trace_ids=list(dict.fromkeys(sources.values())),
            finalization=finalization,
            guard=should_open,
            **(
                {
                    "prepared_trace": draft.trace,
                    "watermark": draft.watermark,
                    "partial_groups": draft.use.partial_groups,
                }
                if prepared_round
                else {}
            ),
        )
        if record is None:
            if sources and reason is None:
                await finalization.persist(
                    self._ignore_unhandled, [e.event_id for e in batch["events"]]
                )
            await self.prewrites.discard(batch, reason)
            for responsibility in batch["finalizations"]:
                responsibility.release()
            return None
        if prepared_round and record["trace_id"] != draft.trace:
            await self.prewrites.discard(batch, "stale")
            prepared_round = None
        trace = record["trace_id"]
        batch["trace"] = trace
        self._bind_finalization(trace, finalization)
        if prepared_round:
            self.prewrites.adopt(batch)
            if draft.use.memories:
                await self.memory.reinforce_used(draft.use.memories)
        await finalization.persist(
            self.ledger.merge,
            [e.event_id for e in batch["events"][:-1] if e.event_id not in sources],
            trace,
        )
        if reaction := batch.get("reaction"):
            await self._react_only(record, finalization, *reaction)
            for responsibility in batch["finalizations"]:
                responsibility.release()
            return trace
        for responsibility in batch["finalizations"][1:]:
            responsibility.release()
        extra = ""
        note = batch.get("catch_up")
        cancel = (
            None
            if note and event.event_id in note.historical
            else fast_cancel(event.message, self.cancel_words())
        )
        # 在同会话队列前执行控制动作，否则无法取消队首正在运行的聊天。
        if cancel is not None:
            with record_scope(trace):
                extra = "取消结果：" + await self.cancel(event, cancel)
        future = await self.kernel.submit(
            trace,
            event.message.sender,
            lambda: self.run(
                trace,
                event.conversation,
                event.message.sender,
                batch["events"],
                mode=batch["mode"],
                prepared_round=prepared_round,
                respond=opinion if isinstance(opinion, Respond) else None,
                extra=extra,
                catch_up=note,
            ),
        )
        batch["queued"] = True
        self.track(self._settle_record(trace, finalization, future))
        return trace

    def cancel_words(self):
        return self.config.get("tasks", {}).get(
            "cancel_words", ["取消", "停", "停止", "停下", "别做了"]
        )

    async def cancel(self, event, args):
        return await self.kernel.cancel(
            self.cid,
            event.message.sender,
            event.conversation.conversation_id,
            reply_to=event.message.reply_to,
            **args,
        )

    async def generate(self, trace, messages, specs, conversation, token, *, rewrite=False):
        token.raise_if_cancelled()
        finalization = self.kernel.finalizations.get(trace)
        owned = finalization is None
        if owned:
            finalization = OwnedFinalization(self.kernel.delivery)
        try:
            return await finalization.follow(
                self._generate(
                    trace, messages, specs, conversation, token, finalization, rewrite=rewrite
                )
            )
        finally:
            if owned:
                finalization.release()

    async def _generate(
        self,
        trace,
        messages,
        specs,
        conversation,
        token,
        finalization,
        *,
        rewrite=False,
        prepared=False,
        deadline=None,
    ):
        token.raise_if_cancelled()
        record = None if prepared else await finalization.persist(self.ledger.get, "record", trace)
        limit = deadline or self.clock.now() + timedelta(
            seconds=self.config.get("agent", {}).get("model_call_seconds", 120)
        )
        if record and record["deadline"]:
            deadline = datetime.fromisoformat(record["deadline"])
            limit = min(limit, deadline) if rewrite else deadline

        def record_model_call():
            with self.ledger.transaction():
                record = self.ledger.get("record", trace)
                record["model_calls"] = record.get("model_calls", 0) + 1
                self.ledger.put("record", trace, record)

        if not prepared:
            await finalization.persist(record_model_call)
        token.raise_if_cancelled()
        child = token.child()
        request = ModelRequest(
            messages=messages,
            tools=specs,
            trace_id=trace,
            budget=Budget(
                deadline=limit,
                max_tool_rounds=self.config.get("agent", {}).get("max_tool_rounds", 4),
            ),
        )
        duration_ms = None

        async def call():
            nonlocal duration_ms
            started = self.clock.monotonic()
            try:
                return await self.model_call(
                    "generate",
                    request,
                    child,
                    scene=conversation.scene,
                    conversation_id=conversation.conversation_id,
                )
            finally:
                duration_ms = round((self.clock.monotonic() - started) * 1000)

        task = asyncio.create_task(call())
        finalization.track(task)
        timer = asyncio.create_task(self.clock.sleep_until(limit))
        cancelled = asyncio.create_task(token.wait())
        watched = False
        result_observed = False
        usage_recorded = False

        def audit_usage(result, timing):
            nonlocal usage_recorded
            if usage_recorded:
                return
            self.ledger.audit(
                "model.usage",
                trace_id=trace,
                purpose="rewrite" if rewrite else "reply",
                plugin=self.config["model"]["plugin"],
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                duration_ms=duration_ms,
                cached_input_tokens=result.usage.cached_input_tokens,
                **({} if rewrite else {"prepared": prepared}),
                image_count=sum(p.type == "image" for m in messages for p in m.parts),
                image_bytes=sum(
                    p.asset.size for m in messages for p in m.parts if p.type == "image" and p.asset
                ),
                finish=result.finish,
                timing=timing,
            )
            usage_recorded = True

        async def late():
            try:
                result = await task
            except (Exception, asyncio.CancelledError):
                return

            def audit_late_model_result():
                with self.ledger.transaction():
                    audit_usage(result, "late")
                    if not prepared:
                        self.ledger.audit(
                            "model.late_result",
                            trace_id=trace,
                            content=result.model_dump(mode="json"),
                        )

            await finalization.persist(audit_late_model_result)

        try:
            done, _ = await asyncio.wait(
                {task, timer, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
            if task not in done:
                child.cancel("cancelled" if token.cancelled else "deadline")
                grace = asyncio.create_task(self.clock.sleep(self.kernel.grace))
                try:
                    await asyncio.wait({task, grace}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    grace.cancel()
                    await asyncio.gather(grace, return_exceptions=True)
                if not task.done():
                    task.cancel()

                finalization.track(self.track(late()))
                watched = True
                if token.cancelled:
                    raise asyncio.CancelledError
                raise TimeoutError("model deadline")
            result = task.result()
            result_observed = True
            if token.cancelled:
                finalization.track(self.track(late()))
                watched = True
                raise asyncio.CancelledError

            def audit_on_time_model_result():
                with self.ledger.transaction():
                    audit_usage(result, "on_time")

            await finalization.persist(audit_on_time_model_result)
            if token.cancelled:
                finalization.track(self.track(late()))
                watched = True
                raise asyncio.CancelledError
            self.protection.update(result.quota)
            return result
        finally:
            if not task.done():
                child.cancel(token.reason or "cancelled")
                task.cancel()
            if not watched and not result_observed:
                finalization.track(self.track(late()))
                watched = True
            timer.cancel()
            cancelled.cancel()
            await asyncio.gather(timer, cancelled, return_exceptions=True)

    @asynccontextmanager
    async def _presence(self, trace, conversation, actor, events):
        from clytie.memory.presence import bind_presence

        actors = set()
        if actor is not None and not self.boundary.paused(actor):
            actors.add(actor.actor_id)
        for event in events:
            if event.message is None or self.boundary.paused(event.message.sender):
                continue
            actors.add(event.message.sender.actor_id)
            actors.update(part.actor_id for part in event.message.parts if part.type == "mention")
            if event.message.reply_to:
                ref = event.message.reply_to.model_dump(mode="json")
                for row in await self.kernel.persist(trace, self.ledger.rows, "inbound"):
                    msg = row.get("message")
                    if (
                        msg
                        and msg["ref"] == ref
                        and row.get("reason") != "boundary_paused"
                        and self.context.visible(row, conversation)
                    ):
                        actors.add(msg["sender"]["actor_id"])
        with bind_presence(conversation.conversation_id, actors):
            yield

    async def run(self, trace, conversation, actor, events, **options):
        async with self._presence(trace, conversation, actor, events):
            return await self._run(trace, conversation, actor, events, **options)

    async def _wait_for_images(self, events):
        ids = {e.event_id for e in events}
        pending = [task for (key, _), task in self.media_jobs.items() if key in ids]
        if pending:
            timer = asyncio.create_task(self.clock.sleep(15))
            complete = asyncio.gather(*pending, return_exceptions=True)
            try:
                await asyncio.wait((timer, complete), return_when=asyncio.FIRST_COMPLETED)
            finally:
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)

    async def _round_context(
        self,
        trace,
        conversation,
        actor,
        events,
        mode,
        extra,
        proactive,
        dialogue,
        rounds,
        picked,
        selected_refs,
        use=None,
        *,
        proactive_tools=False,
        materials,
        catch_up=None,
    ):
        specs, states = await self.tools(
            conversation, actor, proactive, proactive_tools=proactive_tools
        )
        scope_actor = (
            self.admission.private_actors.get(conversation.conversation_id) if proactive else actor
        )
        exhausted = rounds >= self.config.get("agent", {}).get("max_tool_rounds", 4)
        limit = self.config.get("vision", {}).get("max_images", 30)
        images = ImageContext(limit - len(selected_refs))
        images.picked = picked
        context = await self.context.build(
            conversation,
            actor,
            events,
            states,
            mode=mode,
            extra=extra,
            proactive=proactive,
            memory_trace=trace,
            images=images,
            materials=materials,
            catch_up=catch_up,
            available_tools=set() if exhausted else {spec.name for spec in specs},
            **({"use": use} if use is not None else {}),
        )
        request_messages = [*context, *dialogue]
        if (
            materials.has_summary
            and self.features.supports_tools
            and (not proactive or proactive_tools)
            and self.scope_allows(scope_actor, RECALL.name, conversation)
        ):
            specs.append(RECALL)
        images.apply(request_messages)
        if selected_refs:
            request_messages.append(reference_message(selected_refs))
        candidates = self.appearance.dialogue_refs(limit)
        if (
            candidates
            and images.attached
            and self.features.supports_tools
            and (not proactive or proactive_tools)
            and self.scope_allows(scope_actor, "core_view_image", conversation)
            and self.scope_allows(scope_actor, VIEW.name, conversation)
        ):
            specs.append(VIEW)
        # Picks apply to the next request, which has the full image budget again.
        images.limit = limit
        view = view_tool(limit)
        if (
            images.available()
            and self.features.supports_tools
            and (not proactive or proactive_tools)
            and self.scope_allows(scope_actor, view.name, conversation)
        ):
            specs.append(view)
        if exhausted:
            request_messages.append(message("system", "工具轮数已用完，请直接回复"))
        return specs, candidates, images, request_messages

    async def _run(
        self,
        trace,
        conversation,
        actor,
        events,
        *,
        mode=None,
        extra="",
        proactive=False,
        proactive_tools=False,
        direct=None,
        respond=None,
        prepared_round=None,
        catch_up=None,
    ):
        token = self.kernel.tokens.setdefault(trace, CancelToken())
        self.kernel.record_tasks[trace] = asyncio.current_task()
        spoken_names = ()
        try:
            if not proactive and actor is not None and actor.role == "owner":
                spoken_names = self.outbound.names_in_batch(events)
            result_assets, dialogue, rounds = [], [], 0
            materials = Materials()
            picked = []
            selected_refs = []
            if prepared_round is None:
                await self._wait_for_images(events)
            if direct is not None:
                record = await self.kernel.persist(trace, self.ledger.get, "record", trace)
                intents = [self.intent(record, [part], kind="proactive") for part in direct]
                await self.finish(trace, "ok", intents)
                return
            recheck = (
                respond
                and respond.recheck_after
                and conversation.kind == "group"
                and not any(
                    e.addressed
                    and (e.addressed.mention or e.addressed.reply_to_self or e.addressed.name)
                    for e in events
                )
            )
            since = (
                await self.kernel.persist(trace, self.conversation_messages.watermark)
                if recheck
                else None
            )
            while True:
                token.raise_if_cancelled()
                limit = self.config.get("vision", {}).get("max_images", 30)
                exhausted = rounds >= self.config.get("agent", {}).get("max_tool_rounds", 4)
                view = view_tool(limit)
                if prepared_round is not None:
                    specs, candidates, images, turn, materials = prepared_round
                    prepared_round = None
                else:
                    specs, candidates, images, messages = await self._round_context(
                        trace,
                        conversation,
                        actor,
                        events,
                        mode,
                        extra,
                        proactive,
                        dialogue,
                        rounds,
                        picked,
                        selected_refs,
                        proactive_tools=proactive_tools,
                        materials=materials,
                        catch_up=catch_up,
                    )
                    turn = await self.generate(
                        trace, messages, [] if exhausted else specs, conversation, token
                    )
                selected_refs = []
                if turn.finish == "cancelled":
                    token.cancel("model_cancelled")
                    raise asyncio.CancelledError
                if turn.finish in {"error", "deadline"}:
                    raise RuntimeError("model_error")
                if turn.finish != "tool_calls" or exhausted or (proactive and not proactive_tools):
                    text = turn.text or ""
                    break
                rounds += 1

                def record_tool_rounds():
                    with self.ledger.transaction():
                        row = self.ledger.get("record", trace)
                        row["tool_rounds"] = rounds
                        self.ledger.put("record", trace, row)

                await self.kernel.persist(trace, record_tool_rounds)
                dialogue.append(
                    ChatMessage(
                        role="assistant",
                        parts=[TextPart(text=turn.text)] if turn.text else [],
                        tool_calls=turn.tool_calls,
                    )
                )
                for call in turn.tool_calls:
                    token.raise_if_cancelled()
                    if call.tool == RECALL.name and RECALL in specs:
                        result = await self.kernel.persist(
                            trace, self._recall_material, trace, materials, call
                        )
                    elif call.tool == VIEW.name and VIEW in specs:
                        valid = not call.arguments
                        content = "这个工具没有参数"
                        if valid:
                            selected_refs = candidates[: limit - len(images.next_picks)]
                            total = len(self.appearance.references)
                            content = "已附上你的形象参考图"
                            if not selected_refs:
                                valid = False
                                content = "本轮图片名额已用于点选的图片，请在下一轮对照形象。"
                            elif len(selected_refs) < total:
                                content = f"附上了 {total} 张参考图中的 {len(selected_refs)} 张"
                        await self.kernel.persist(trace, self._audit_view_appearance, trace, valid)
                        result = ToolResult(
                            data={},
                            call_id=call.call_id,
                            status="ok" if valid else "error",
                            content=content,
                        )
                    elif call.tool == view.name and view in specs:
                        numbers = call.arguments.get("numbers")
                        images.limit = limit - len(selected_refs)
                        ok = set(call.arguments) == {"numbers"} and images.choose(numbers)
                        if ok:
                            picked = images.next_picks
                        await self.kernel.persist(
                            trace, self._audit_image_numbers, trace, numbers if ok else []
                        )
                        result = ToolResult(
                            data={},
                            call_id=call.call_id,
                            status="ok" if ok else "error",
                            content="已附上 " + "、".join(f"#{n}" for n in numbers)
                            if ok
                            else "图片编号无效或本轮剩余图片名额不足"
                            if selected_refs
                            else "图片编号无效",
                        )
                    else:
                        result = await self.execute(
                            trace,
                            conversation,
                            actor,
                            call,
                            specs,
                            token,
                            proactive=proactive_tools,
                        )
                    if result.status == "ok" and result.assets:
                        result_assets.append(result.assets)
                    content = result.content
                    budget = self.config.get("context", {}).get("material_budget_chars", 6000)
                    if len(content) > budget and call.tool != RECALL.name:
                        content = content[:budget] + "[已截断]"
                    if result.material:
                        content = NOTICE + "\n" + content
                    dialogue.append(
                        ChatMessage(
                            role="tool", tool_call_id=call.call_id, parts=[TextPart(text=content)]
                        )
                    )
            if proactive and not text:

                def audit_proactive_decline():
                    with self.ledger.transaction():
                        row = self.ledger.get("record", trace)
                        if row.get("proactive_result") == "superseded":
                            return
                        row["proactive_result"] = "declined"
                        self.ledger.put("record", trace, row)
                        self.ledger.audit("proactive_declined", trace_id=trace)

                await self.kernel.persist(trace, audit_proactive_decline)
                await self.finish(trace, "ok", [])
                return
            intents = await self.outgoing(
                trace,
                conversation,
                actor,
                events,
                text,
                result_assets,
                token,
                spoken_names=spoken_names,
                proactive=proactive,
                respond=respond,
                since=since,
            )
            token.raise_if_cancelled()
            await self.finish(trace, "ok", intents)
        except asyncio.CancelledError:
            if not token.cancelled:
                raise
            await self.kernel.seal(trace, "cancelled", error={"code": "cancelled", "message": ""})
        except (StorageBusy, StorageCancelled, StorageFailed) as exc:
            code = exc.code

            def audit_storage_failure():
                with self.ledger.transaction():
                    self.ledger.audit("agent.failed", trace_id=trace, code=code)

            await self.kernel.persist(trace, audit_storage_failure)
            await self.kernel.seal(trace, "error", error={"code": code, "message": ""})
        except Exception as exc:
            code = (
                "outbound_blocked"
                if str(exc) == "outbound_blocked"
                else "model_error"
                if isinstance(exc, (TimeoutError, RuntimeError))
                else "agent_error"
            )
            self.log.warning("agent.failed", trace_id=trace, code=code)

            def audit_failure():
                with self.ledger.transaction():
                    self.ledger.audit("agent.failed", trace_id=trace, code=code)

            await self.kernel.persist(trace, audit_failure)
            intents = []
            if not proactive and conversation.kind == "private" and not token.cancelled:
                text = self.config.get("replies", {}).get(
                    "fallback_private", "刚刚没接上，可以再说一次吗？"
                )
                if self.model_health == "auth_invalid" or self.kernel.delivery.silenced:
                    text = ""
                if text and not await self.outbound.check(
                    text, conversation.scene, spoken_names=spoken_names
                ):
                    try:
                        intents = await self.outgoing(
                            trace,
                            conversation,
                            actor,
                            events,
                            text,
                            [],
                            token,
                            spoken_names=spoken_names,
                            fallback=True,
                        )
                    except (StorageBusy, StorageCancelled, StorageFailed) as failure:
                        code, intents = failure.code, []
                    except Exception:
                        self.log.warning("fallback.failed", trace_id=trace)
                        intents = []
            await self.finish(
                trace,
                "cancelled" if token.cancelled else "error",
                intents if not token.cancelled else [],
                error={"code": code, "message": ""},
            )
        finally:
            self.kernel.record_tasks.pop(trace, None)
            self.memory.sessions.pop(trace, None)
            self.kernel.reactions.forget(trace)

    async def execute(self, trace, conversation, actor, call, specs, token, *, proactive=False):
        declaration = self.declarations().get(call.tool)
        heavy = declaration is not None and declaration[1].get("heavy", False)
        result = await self._execute_tool(
            trace, conversation, actor, call, specs, token, proactive=proactive
        )
        if heavy:

            def record_outcome():
                with self.ledger.transaction():
                    self.ledger.audit("tool.heavy_result", trace_id=trace, status=result.status)

            await self.kernel.persist(trace, record_outcome)
            self.kernel.reactions.record_heavy_result(trace, result.status)
        return result

    async def _execute_tool(
        self, trace, conversation, actor, call, specs, token, *, proactive=False
    ):
        permission_actor = (
            self.admission.private_actors.get(conversation.conversation_id) if proactive else actor
        )
        if not self.scope_allows(permission_actor, call.tool, conversation):
            return ToolResult(
                data={},
                call_id=call.call_id,
                status="denied",
                content=REASONS["permission"],
                error={"code": "permission", "message": ""},
            )
        spec = next((s for s in specs if s.name == call.tool), None)
        if spec is None:
            reason = (
                self.reason(permission_actor, call.tool, conversation)
                if call.tool in self.declarations()
                else "unknown_tool"
            )
            reason = reason or "unknown_tool"
            if proactive and call.tool in self.config.get("proactive", {}).get("tools", {}):
                limit = self.config["proactive"]["tools"][call.tool]["daily"]
                counts = await self.kernel.persist(trace, self.ledger.proactive_counts)
                if counts.get(call.tool, 0) >= limit:
                    reason = "proactive_limit"
            return ToolResult(
                data={},
                call_id=call.call_id,
                status="error" if reason == "unknown_tool" else "denied",
                content=REASONS.get(reason, reason),
                error={"code": reason, "message": ""},
            )
        schema = spec.model_dump(mode="json")["parameters"]
        errors = Draft202012Validator(schema).iter_errors(call.model_dump(mode="json")["arguments"])
        invalid = next(errors, None)
        if invalid is not None:
            return ToolResult(
                data={},
                call_id=call.call_id,
                status="error",
                content=invalid_argument_location(schema, invalid),
                error={"code": "invalid_arguments", "message": ""},
            )
        if call.tool.startswith("core_"):
            if call.tool in {"core_remember", "core_forget", "core_recall"}:
                return await self.memory.execute(
                    trace,
                    conversation,
                    actor,
                    call,
                    token,
                    finalization=self.kernel.finalizations.get(trace),
                )
            if call.tool == "core_cancel_task":
                content = await self.kernel.cancel(
                    self.cid,
                    actor,
                    conversation.conversation_id,
                    task_id=call.arguments["task_id"].lower(),
                )
            else:
                content = await self.boundary.report(
                    trace, actor, call.arguments["category"], token
                )

            def audit_core_tool():
                with self.ledger.transaction():
                    self.ledger.audit(
                        "core.tool",
                        trace_id=trace,
                        tool=call.tool,
                        arguments=call.model_dump(mode="json")["arguments"],
                        result=content,
                    )

            await self.kernel.persist(trace, audit_core_tool)
            return ToolResult(data={}, call_id=call.call_id, status="ok", content=content)
        reg = next(r for r in self.host.registrations(self.cid, "tool") if r.key == call.tool)
        inv = Invocation(
            companion_id=self.cid,
            actor=actor,
            conversation=conversation,
            trace_id=trace,
            origin="proactive" if proactive else "message",
        )
        result = await self.kernel.execute(
            trace,
            reg.plugin_id,
            call,
            Capability(
                self.host,
                self.cid,
                reg,
                self.config.get("context", {}).get("material_budget_chars", 6000),
            ),
            inv,
            permission_actor=permission_actor,
            schema=spec.model_dump(mode="json")["parameters"],
            timeout_s=reg.declaration.get("timeout_s"),
            light=reg.declaration.get("light", False),
            proactive_limit=self.config["proactive"]["tools"][call.tool]["daily"]
            if proactive
            else None,
        )
        if isinstance(result, Denial):
            return ToolResult(
                data={},
                call_id=call.call_id,
                status="denied",
                content=result.message_for_model,
                error={"code": "proactive_limit", "message": ""}
                if result.reason_code == "proactive_limit"
                else None,
            )
        if isinstance(result, dict):
            if result["result"]:
                return ToolResult.model_validate(result["result"]).model_copy(
                    update={"call_id": call.call_id}
                )
            return ToolResult(
                data={},
                call_id=call.call_id,
                status="ok",
                content="这件事已经在做了 " + str(result["task_id"]),
            )
        return result

    def _recall_material(self, trace, materials, call):
        with self.ledger.transaction():
            result = materials.recall(self.ledger, call)
            number = call.arguments.get("number")
            self.ledger.audit(
                "core.tool",
                trace_id=trace,
                tool=call.tool,
                number=number if type(number) is int else None,
                status=result.status,
            )
        return result

    def _audit_view_appearance(self, trace, accepted):
        with self.ledger.transaction():
            self.ledger.audit("core.tool", trace_id=trace, tool=VIEW.name, accepted=accepted)

    def _audit_image_numbers(self, trace, numbers):
        with self.ledger.transaction():
            self.ledger.audit(
                "core.tool", trace_id=trace, tool="core_view_image", numbers=list(numbers)
            )

    def intent(self, record, parts, *, kind="reply", **kwargs):
        return DeliveryIntent(
            intent_id=uuid4().hex,
            companion_id=self.cid,
            conversation_id=record["conversation_id"],
            channel_id=record["channel_id"],
            kind=kind,
            parts=parts,
            trace_id=record["trace_id"],
            task_id=record["task_id"],
            **kwargs,
        )

    async def outgoing(
        self,
        trace,
        conversation,
        actor,
        events,
        text,
        result_assets,
        token,
        *,
        spoken_names,
        proactive=False,
        fallback=False,
        respond=None,
        since=None,
    ):
        record = await self.kernel.persist(trace, self.ledger.get, "record", trace)
        focus = respond.focus if respond else None
        interleaved = 0
        if focus:
            position = await self.kernel.persist(
                trace, self.conversation_messages.focus_position, conversation, focus
            )
            if position is None:
                focus = None
            else:
                interleaved = len(
                    await self.kernel.persist(
                        trace, self.conversation_messages.messages_after, conversation, position
                    )
                )
        draft = ReplyDraft(
            kind="proactive" if proactive else "reply",
            focus=focus,
            interleaved=interleaved,
            parts=[TextPart(text=text)] if text else [],
            conversation=conversation,
            task_id=record["task_id"],
            trace_id=trace,
            form="plain",
        )
        draft = await self.host.behavior(self.cid, "after_generation", draft)
        hits = await self.outbound.check(
            text_parts(draft.parts), conversation.scene, spoken_names=spoken_names
        )
        if hits and not fallback:
            specs, states = await self.tools(conversation, actor, proactive)
            images = ImageContext(self.config.get("vision", {}).get("max_images", 30))
            context = await self.context.build(
                conversation,
                actor,
                events,
                states,
                proactive=proactive,
                memory_trace=trace,
                images=images,
            )
            images.apply(context)
            context.append(
                message(
                    "system",
                    "仅改掉以下内容，保持原意和语气；只返回改写文字："
                    + json.dumps(hits, ensure_ascii=False),
                )
            )
            if "包含了不能在这里说的私密内容" not in hits.get("privacy", []):
                context.append(message("user", text_parts(draft.parts)))
            try:
                turn = await self.generate(trace, context, [], conversation, token, rewrite=True)
                if turn.finish == "stop":
                    draft = ReplyDraft(
                        kind="proactive" if proactive else "reply",
                        focus=focus,
                        interleaved=interleaved,
                        parts=[TextPart(text=turn.text or "")],
                        conversation=conversation,
                        task_id=record["task_id"],
                        trace_id=trace,
                        form="plain",
                    )
                    draft = await self.host.behavior(self.cid, "after_generation", draft)
                    hits = await self.outbound.check(
                        text_parts(draft.parts), conversation.scene, spoken_names=spoken_names
                    )
            except (StorageBusy, StorageCancelled, StorageFailed):
                raise
            except (TimeoutError, RuntimeError):
                pass

            def record_rewrite():
                with self.ledger.transaction():
                    row = self.ledger.get("record", trace)
                    row["rewritten"] = True
                    self.ledger.put("record", trace, row)

            await self.kernel.persist(trace, record_rewrite)

        def record_outbound_categories():
            with self.ledger.transaction():
                row = self.ledger.get("record", trace)
                row["outbound_categories"] = list(hits)
                self.ledger.put("record", trace, row)

        await self.kernel.persist(trace, record_outbound_categories)
        if "privacy" in hits:
            raise ValueError("outbound_blocked")
        if hits:
            self.log.info("outbound.style", trace_id=trace)
        if since is not None:
            recent = await self.kernel.persist(
                trace, self.conversation_messages.messages_after, conversation, since
            )
            if len(recent) >= respond.recheck_after and not await self.judgments.recheck(
                conversation,
                recent,
                text_parts(draft.parts),
                trace=trace,
                token=token,
                deadline=datetime.fromisoformat(record["deadline"])
                if record.get("deadline")
                else None,
            ):

                def withdrawn():
                    with self.ledger.transaction():
                        self.ledger.audit("reply_withdrawn", trace_id=trace)

                await self.kernel.persist(trace, withdrawn)
                return []
        kind = "proactive" if proactive else "reply"
        groups = [[p] for p in draft.parts] if draft.form == "split" else [list(draft.parts)]
        groups = [g for g in groups if text_parts(g)]
        group = uuid4().hex
        intents = [
            self.intent(
                record,
                parts,
                kind=kind,
                target=draft.quote if i == 0 else None,
                split={"group_id": group, "index": i, "total": len(groups)}
                if len(groups) > 1
                else None,
            )
            for i, parts in enumerate(groups)
        ]
        if result_assets:
            features = self.kernel.delivery.channels[record["channel_id"]].features()
            for assets in result_assets:
                for parts in asset_part_groups(assets, features):
                    intents.append(self.intent(record, parts, kind=kind))
        if record["task_id"] and intents and not proactive:
            start = (
                record.get("execution_started_at")
                or record.get("started_at")
                or record["created_at"]
            )
            card = render_task_card(
                self.config,
                record["task_id"],
                (self.clock.now() - datetime.fromisoformat(start)).total_seconds(),
                scene=conversation.scene,
            )
            card_hits = await self.outbound.check(card, conversation.scene)
            if "privacy" in card_hits:
                self.log.warning("task_card.private_name", trace_id=trace)
                self.warnings.append("task_card.private_name: configure external name")
                await self.boundary.add_warning("task_card_private_name")
            elif groups:
                index = len(groups) - 1
                intents[index] = intents[index].model_copy(
                    update={"parts": [*intents[index].parts, TextPart(text="\n" + card)]}
                )
            else:
                intents.append(self.intent(record, [TextPart(text=card)], kind=kind))
        return await self.host.behavior(self.cid, "before_send", intents, scene=conversation.scene)

    async def finish(self, trace, status, intents, error=None):
        await self.kernel.seal(trace, status, intents, error)
        await self.kernel.delivery.deliver(trace, finalization=self.kernel.finalizations.get(trace))

    async def submit(
        self,
        conversation_id,
        *,
        topic=None,
        material=None,
        parts=None,
        reason,
        tools=False,
        plugin_id=None,
    ):
        finalization = OwnedFinalization(self.kernel.delivery)
        state = {"handed": False}

        async def submit_owned():
            try:
                return await self._submit(
                    conversation_id,
                    finalization,
                    state,
                    topic=topic,
                    material=material,
                    parts=parts,
                    reason=reason,
                    tools=tools,
                    plugin_id=plugin_id,
                )
            except BaseException as exc:
                if state.get("trace") and not state["handed"]:
                    await self.kernel.seal(
                        state["trace"],
                        "cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                        error={"code": getattr(exc, "code", "agent_error"), "message": ""},
                    )
                raise
            finally:
                if not state["handed"]:
                    finalization.release()

        return await self._intake(submit_owned(), finalization)

    async def _submit(
        self,
        conversation_id,
        finalization,
        state,
        *,
        topic=None,
        material=None,
        parts=None,
        reason,
        tools=False,
        plugin_id=None,
    ):
        if (await self.proactive_pause.status())["paused"]:

            def audit_pause():
                with self.ledger.transaction():
                    self.ledger.audit("proactive.paused", plugin=plugin_id, reason=reason)

            await run_sync(self.ledger, audit_pause)
            return ProactiveSubmission(status="paused")
        if (
            self.shutting_down
            or self.kernel.delivery.silenced
            or self.model_health == "auth_invalid"
        ):
            trace = uuid4().hex

            def audit_proactive_drop():
                with self.ledger.transaction():
                    self.ledger.audit("proactive.dropped", trace_id=trace)

            await run_sync(self.ledger, audit_proactive_drop)
            return ProactiveSubmission(status="paused")
        validate_proactive(topic=topic, material=material, parts=parts, tools=tools)
        target = self.instance.get("conversations", {}).get(conversation_id)
        if target is None or target["scene"] == "stranger_private":
            raise ValueError("proactive target denied")
        if tools:
            recipient = self.admission.private_actors.get(conversation_id)
            if target["kind"] != "private" or recipient is None or recipient.role != "owner":
                raise ValueError("proactive tools require a registered owner private conversation")
        channel = next(
            (c["account"] for c in self.config["channels"] if c["plugin"] == target["channel"]),
            None,
        )
        if channel is None:
            raise ValueError("channel unavailable")
        conversation = Conversation(
            conversation_id=conversation_id,
            channel_id=channel,
            kind=target["kind"],
            scene=target["scene"],
        )

        async def busy(busy_reason=reason):
            def audit_busy():
                with self.ledger.transaction():
                    self.ledger.audit("proactive.busy", plugin=plugin_id, reason=busy_reason)

            await finalization.persist(audit_busy)
            return ProactiveSubmission(status="busy")

        if channel_state(self.kernel.delivery.channels.get(channel)).status != "online":
            return await busy("platform_offline")
        if conversation_id in self.opening or any(
            key[0] == conversation_id for key in self.batches
        ):
            return await busy()
        # No await between the check and the reservation. Respond waits until the
        # accepted record is queued. The writer also checks active records atomically.
        async with self._opening_record(conversation_id):
            record = await self.kernel.open_record(
                conversation=conversation,
                proactive=True,
                proactive_plugin=plugin_id,
            )
            if record is None:
                return await busy()
            trace = record["trace_id"]
            state["trace"] = trace
            self._bind_finalization(trace, finalization)

            def audit_proactive_submission():
                with self.ledger.transaction():
                    self.ledger.audit(
                        "proactive.submitted", trace_id=trace, reason=reason, plugin=plugin_id
                    )

            await finalization.persist(audit_proactive_submission)
            self.log.info("proactive.submitted", trace_id=trace, reason=reason)
            cid = conversation.conversation_id
            self.proactive_writing[cid] = trace

            def forget():
                if self.proactive_writing.get(cid) == trace:
                    self.proactive_writing.pop(cid)

            finalization.on_complete(forget)
            extra = "这是你主动开口，不是在回复谁。话题和材料仅为内部说明：" + json.dumps(
                {"topic": topic, "material": material}, ensure_ascii=False
            )
            future = await self.kernel.submit(
                trace,
                None,
                lambda: self.run(
                    trace,
                    conversation,
                    None,
                    [],
                    extra=extra,
                    proactive=True,
                    direct=parts,
                    proactive_tools=tools,
                ),
            )
            state["handed"] = True
            self.track(self._settle_record(trace, finalization, future))
            return ProactiveSubmission(status="accepted", trace_id=trace)

    async def proactive_result(self, plugin_id, trace_id):
        return await run_sync(self.ledger, self.ledger.proactive_result, plugin_id, trace_id)

    async def drain(self):
        while self.intake_tasks:
            await asyncio.gather(*tuple(self.intake_tasks), return_exceptions=True)
        while self.opening:
            await asyncio.gather(*(asyncio.shield(f) for f in tuple(self.opening.values())))
        await self.kernel.queue.drain()
        await self.safety_belt.drain()
        await self.kernel.reactions.drain()
        if self.media_jobs:
            await asyncio.gather(*tuple(self.media_jobs.values()), return_exceptions=True)
        if self.image_fetcher.workers:
            await asyncio.gather(*tuple(self.image_fetcher.workers), return_exceptions=True)
