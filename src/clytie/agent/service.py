"""入站编排、模型工具循环、主动话题与共用出站流水线。"""

import asyncio
import json
from datetime import datetime, timedelta
from uuid import uuid4

from jsonschema import Draft202012Validator

from clytie.config import thaw
from clytie.kernel.events import background
from clytie.kernel.ledger import EXEC_TERMINAL
from clytie.kernel.service import record_scope
from clytie.kernel.task_card import render_task_card
from clytie.plugin_host.memory import bind_scene
from clytie.sdk import CancelToken
from clytie.sdk.behavior import Defer, Ignore, Mode, ReactOnly, Skip
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

from .context import Context, message
from .persona import Persona
from .policy import CATEGORIES, Boundary, Outbound, Protection, fast_cancel, text_parts

REASONS = {
    "permission": "对方没有使用这个能力的权限",
    "protection_mode": "额度紧张，暂停非主人的重任务",
    "disabled": "这个能力已停用",
    "start_failed": "这个能力暂时维护中",
    "migrating": "这个能力暂时维护中",
    "model_no_tools": "当前模型不支持使用工具",
}


class Capability:
    def __init__(self, host, cid, reg):
        self.host, self.cid, self.reg = host, cid, reg

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
        return await self.host.invoke(
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
        self.ledger, self.clock, self.cid = kernel.ledger, kernel.clock, kernel.ledger.companion_id
        self.log = kernel.log
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
        self.filtered_count = 0
        self.model_health = "unknown"
        self.model_error_code = None
        self.shutting_down = False
        kernel.config = config
        kernel.permissions = lambda actor, tool: (
            self.reason(actor, tool)
            not in {"permission", "disabled", "start_failed", "migrating", "model_no_tools"}
        )
        kernel.quota = lambda actor, tool: self.reason(actor, tool) != "protection_mode"
        kernel.conversation_scenes.update(
            {key: value["scene"] for key, value in instance.get("conversations", {}).items()}
        )

    async def model_call(self, method, *args, scene="public_group", conversation_id=None):
        from clytie.sdk.types import ModelTurn

        if method == "generate" and (
            self.model_health == "auth_invalid" or self.kernel.delivery.silenced
        ):
            return ModelTurn(
                finish="error",
                usage={},
                error={
                    "code": "auth_invalid" if self.model_health == "auth_invalid" else "silenced",
                    "message": "",
                },
            )
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
        if method == "generate":
            self.model_error_code = result.error.code if result.error else None
            if self.model_error_code == "auth_invalid":
                self.model_health = "auth_invalid"
            elif self.model_health != "auth_invalid":
                self.model_health = "ok"
        return result

    def rebuild_memory(self):
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
        if self.features.agent_type:
            raise ValueError("unsupported_model_type")
        self.protection.update(await self.model_call("quota"))
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

    def track(self, coroutine):
        task = background(coroutine)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

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

    def reason(self, actor, tool):
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
        reg = next((r for r in self.host.registrations(self.cid, "tool") if r.key == tool), None)
        if reg is None:
            state = self.host.states.get(self.cid, {}).get(pid, {}).get("status", "disabled")
            return state if state in {"start_failed", "migrating"} else "disabled"
        if actor.role != "owner" and declaration.get("heavy") and self.protection.active:
            return "protection_mode"
        return None

    def cancellable(self, conversation, actor=None):
        return [
            r
            for r in self.ledger.rows("record")
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

    def tools(self, conversation, actor, proactive=False):
        if proactive:
            return [], "主动开口时暂不使用能力"
        specs, lines = [], []
        for name, (_, declaration) in self.declarations().items():
            reason = self.reason(actor, name)
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
        specs.extend(self.memory.specifications(actor))
        if self.cancellable(conversation):
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
        if actor.role not in {"owner", "peer"} and categories:
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
        return specs, "\n".join(lines)

    async def receive(self, event):
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
                key, new = self.ledger.claim_inbound(event)
                if new:
                    self.ledger.ignore(key, reason)
            return None
        if hasattr(self, "recollector"):
            self.recollector.activity(event.conversation)
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
                key, new = self.ledger.claim_inbound(event)
                if new:
                    self.ledger.ignore(key, "boundary_paused")
            return None
        if not event.dedupable:
            await self.host.behavior(self.cid, "message_received", event)
            return None
        key, new = self.ledger.claim_inbound(event)
        if not new:
            self.kernel.duplicates += 1
            return None
        if not event.message:
            await self.host.behavior(self.cid, "message_received", event)
            self.ledger.ignore(key)
            return None
        mentioned = any(p.type == "mention" and p.is_self for p in event.message.parts)
        cancel = fast_cancel(
            event.message,
            self.config.get("tasks", {}).get(
                "cancel_words", ["取消", "停", "停止", "停下", "别做了"]
            ),
        )
        if (
            event.conversation.kind == "group"
            and not mentioned
            and cancel is not None
            and self.cancellable(event.conversation, event.message.sender)
        ):
            await self.cancel(event, cancel)
            self.ledger.ignore(key, "cancel_only")
            return None
        await self.host.behavior(self.cid, "message_received", event)
        before = await self.host.behavior(
            self.cid, "before_attention", (event, self.host.companion_state(self.cid))
        )
        if isinstance(before, Ignore):
            self.ledger.ignore(key)
            return None
        if isinstance(before, ReactOnly):
            record = self.kernel.open_record(event_id=key)
            intent = self.intent(
                record, [], kind="react", emoji=before.emoji, target=event.message.ref
            )
            # 宿主已检验插件 message.react；owner Ignore 降级是本体例外。
            self.kernel.allow_reaction_exception(
                record["trace_id"], intent, permissions=("message.react",)
            )
            await self.kernel.seal(record["trace_id"], "ok", [intent])
            await self.kernel.delivery.deliver(record["trace_id"])
            return record["trace_id"]
        batch_key = (event.conversation.conversation_id, event.message.sender.actor_id)
        if batch_key in self.batches:
            self.batches[batch_key]["events"].append(event)
            return None
        batch = {
            "events": [event],
            "started": self.clock.monotonic(),
            "mode": before if isinstance(before, Mode) else None,
        }
        self.batches[batch_key] = batch
        return await self.attention(batch_key, mentioned)

    async def attention(self, key, mentioned):
        batch = self.batches[key]
        event = batch["events"][-1]
        opinion = await self.host.behavior(
            self.cid,
            "attention",
            (event, [e.message for e in batch["events"]]),
            mentioned=mentioned,
        )
        if isinstance(opinion, Defer) and self.clock.monotonic() - batch["started"] < 120:
            delay = min(opinion.seconds, 120 - (self.clock.monotonic() - batch["started"]))

            async def later():
                await self.clock.sleep(delay)
                await self.attention(
                    key,
                    any(
                        p.type == "mention" and p.is_self
                        for e in batch["events"]
                        for p in e.message.parts
                    ),
                )

            self.track(later())
            return None
        self.batches.pop(key)
        if isinstance(opinion, Skip):
            for e in batch["events"]:
                self.ledger.ignore(e.event_id)
            return None
        record = self.kernel.open_record(event_id=event.event_id)
        trace = record["trace_id"]
        self.ledger.merge([e.event_id for e in batch["events"][:-1]], trace)
        extra = ""
        cancel = fast_cancel(
            event.message,
            self.config.get("tasks", {}).get(
                "cancel_words", ["取消", "停", "停止", "停下", "别做了"]
            ),
        )
        # 在同会话队列前执行控制动作，否则无法取消队首正在运行的聊天。
        if cancel is not None:
            with record_scope(trace):
                extra = "取消结果：" + await self.cancel(event, cancel)
        self.kernel.submit(
            trace,
            event.message.sender,
            lambda: self.run(
                trace,
                event.conversation,
                event.message.sender,
                batch["events"],
                mode=batch["mode"],
                extra=extra,
            ),
        )
        return trace

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
        record = self.ledger.get("record", trace)
        limit = self.clock.now() + timedelta(
            seconds=self.config.get("agent", {}).get("model_call_seconds", 120)
        )
        if record["deadline"]:
            deadline = datetime.fromisoformat(record["deadline"])
            limit = min(limit, deadline) if rewrite else deadline
        child = token.child()
        task = asyncio.create_task(
            self.model_call(
                "generate",
                ModelRequest(
                    messages=messages,
                    tools=specs,
                    trace_id=trace,
                    budget=Budget(
                        deadline=limit,
                        max_tool_rounds=self.config.get("agent", {}).get("max_tool_rounds", 4),
                    ),
                ),
                child,
                scene=conversation.scene,
                conversation_id=conversation.conversation_id,
            )
        )
        timer = asyncio.create_task(self.clock.sleep_until(limit))
        cancelled = asyncio.create_task(token.wait())
        watched = False

        async def late():
            try:
                result = await task
            except (Exception, asyncio.CancelledError):
                return
            with self.ledger.transaction():
                self.ledger.audit(
                    "model.late_result", trace_id=trace, content=result.model_dump(mode="json")
                )

        with self.ledger.transaction():
            record["model_calls"] = record.get("model_calls", 0) + 1
            self.ledger.put("record", trace, record)
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

                self.track(late())
                watched = True
                if token.cancelled:
                    raise asyncio.CancelledError
                raise TimeoutError("model deadline")
            result = task.result()
            if token.cancelled:
                self.track(late())
                watched = True
                raise asyncio.CancelledError
            self.protection.update(result.quota)
            return result
        finally:
            if not task.done():
                child.cancel(token.reason or "cancelled")
                task.cancel()
                if not watched:
                    self.track(late())
            timer.cancel()
            cancelled.cancel()
            await asyncio.gather(timer, cancelled, return_exceptions=True)

    async def run(self, trace, conversation, actor, events, **options):
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
                for row in self.ledger.rows("inbound"):
                    msg = row.get("message")
                    if (
                        msg
                        and msg["ref"] == ref
                        and row.get("reason") != "boundary_paused"
                        and self.context.visible(row, conversation)
                    ):
                        actors.add(msg["sender"]["actor_id"])
        with bind_presence(conversation.conversation_id, actors):
            return await self._run(trace, conversation, actor, events, **options)

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
        direct=None,
    ):
        token = self.kernel.tokens.setdefault(trace, CancelToken())
        self.kernel.record_tasks[trace] = asyncio.current_task()
        try:
            assets, dialogue, rounds = [], [], 0
            if direct is not None:
                intents = [
                    self.intent(self.ledger.get("record", trace), [part], kind="proactive")
                    for part in direct
                ]
                await self.finish(trace, "ok", intents)
                return
            while True:
                token.raise_if_cancelled()
                specs, states = self.tools(conversation, actor, proactive)
                exhausted = rounds >= self.config.get("agent", {}).get("max_tool_rounds", 4)
                context = await self.context.build(
                    conversation,
                    actor,
                    events,
                    states,
                    mode=mode,
                    extra=extra,
                    proactive=proactive,
                    memory_trace=trace,
                )
                if exhausted:
                    context.append(message("system", "工具轮数已用完，请直接回复"))
                turn = await self.generate(
                    trace, [*context, *dialogue], [] if exhausted else specs, conversation, token
                )
                if turn.finish == "cancelled":
                    token.cancel("model_cancelled")
                    raise asyncio.CancelledError
                if turn.finish in {"error", "deadline"}:
                    raise RuntimeError("model_error")
                if turn.finish != "tool_calls" or exhausted or proactive:
                    text = turn.text or ""
                    break
                rounds += 1
                with self.ledger.transaction():
                    row = self.ledger.get("record", trace)
                    row["tool_rounds"] = rounds
                    self.ledger.put("record", trace, row)
                dialogue.append(
                    ChatMessage(
                        role="assistant",
                        parts=[TextPart(text=turn.text)] if turn.text else [],
                        tool_calls=turn.tool_calls,
                    )
                )
                for call in turn.tool_calls:
                    token.raise_if_cancelled()
                    result = await self.execute(trace, conversation, actor, call, specs, token)
                    if result.status == "ok":
                        assets.extend(result.assets)
                    content = result.content
                    budget = self.config.get("context", {}).get("material_budget_chars", 6000)
                    if len(content) > budget:
                        content = content[:budget] + "[已截断]"
                    dialogue.append(
                        ChatMessage(
                            role="tool", tool_call_id=call.call_id, parts=[TextPart(text=content)]
                        )
                    )
            if proactive and not text:
                with self.ledger.transaction():
                    self.ledger.audit("proactive_declined", trace_id=trace)
            intents = await self.outgoing(
                trace, conversation, actor, events, text, assets, token, proactive=proactive
            )
            token.raise_if_cancelled()
            await self.finish(trace, "ok", intents)
        except asyncio.CancelledError:
            if not token.cancelled:
                raise
            await self.kernel.seal(trace, "cancelled", error={"code": "cancelled", "message": ""})
        except Exception as exc:
            code = (
                "outbound_blocked"
                if str(exc) == "outbound_blocked"
                else "model_error"
                if isinstance(exc, (TimeoutError, RuntimeError))
                else "agent_error"
            )
            self.log.warning("agent.failed", trace_id=trace, code=code)
            with self.ledger.transaction():
                self.ledger.audit("agent.failed", trace_id=trace, code=code)
            intents = []
            if not proactive and conversation.kind == "private" and not token.cancelled:
                text = self.config.get("replies", {}).get(
                    "fallback_private", "刚刚没接上，可以再说一次吗？"
                )
                if self.model_health == "auth_invalid" or self.kernel.delivery.silenced:
                    text = ""
                if text and not self.outbound.check(text, conversation.scene):
                    try:
                        intents = await self.outgoing(
                            trace, conversation, actor, events, text, [], token, fallback=True
                        )
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

    async def execute(self, trace, conversation, actor, call, specs, token):
        spec = next((s for s in specs if s.name == call.tool), None)
        if spec is None:
            reason = (
                self.reason(actor, call.tool)
                if call.tool in self.declarations()
                else "unknown_tool"
            )
            reason = reason or "unknown_tool"
            return ToolResult(
                data={},
                call_id=call.call_id,
                status="error" if reason == "unknown_tool" else "denied",
                content=REASONS.get(reason, reason),
                error={"code": reason, "message": ""},
            )
        if list(
            Draft202012Validator(spec.model_dump(mode="json")["parameters"]).iter_errors(
                call.model_dump(mode="json")["arguments"]
            )
        ):
            return ToolResult(
                data={},
                call_id=call.call_id,
                status="error",
                content="invalid_arguments",
                error={"code": "invalid_arguments", "message": ""},
            )
        if call.tool.startswith("core_"):
            if call.tool in {"core_remember", "core_forget", "core_recall"}:
                return await self.memory.execute(trace, conversation, actor, call, token)
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
            with self.ledger.transaction():
                self.ledger.audit(
                    "core.tool",
                    trace_id=trace,
                    tool=call.tool,
                    arguments=call.model_dump(mode="json")["arguments"],
                    result=content,
                )
            return ToolResult(data={}, call_id=call.call_id, status="ok", content=content)
        reg = next(r for r in self.host.registrations(self.cid, "tool") if r.key == call.tool)
        inv = Invocation(
            companion_id=self.cid, actor=actor, conversation=conversation, trace_id=trace
        )
        result = await self.kernel.execute(
            trace,
            reg.plugin_id,
            call,
            Capability(self.host, self.cid, reg),
            inv,
            schema=spec.model_dump(mode="json")["parameters"],
            timeout_s=reg.declaration.get("timeout_s"),
        )
        if isinstance(result, Denial):
            return ToolResult(
                data={}, call_id=call.call_id, status="denied", content=result.message_for_model
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
        assets,
        token,
        *,
        proactive=False,
        fallback=False,
    ):
        record = self.ledger.get("record", trace)
        draft = ReplyDraft(
            parts=[TextPart(text=text)] if text else [],
            conversation=conversation,
            task_id=record["task_id"],
            trace_id=trace,
            form="plain",
        )
        draft = await self.host.behavior(self.cid, "after_generation", draft)
        hits = self.outbound.check(text_parts(draft.parts), conversation.scene)
        if hits and not fallback:
            specs, states = self.tools(conversation, actor, proactive)
            context = await self.context.build(
                conversation, actor, events, states, proactive=proactive, memory_trace=trace
            )
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
                        parts=[TextPart(text=turn.text or "")],
                        conversation=conversation,
                        task_id=record["task_id"],
                        trace_id=trace,
                        form="plain",
                    )
                    draft = await self.host.behavior(self.cid, "after_generation", draft)
                    hits = self.outbound.check(text_parts(draft.parts), conversation.scene)
            except (TimeoutError, RuntimeError):
                pass
            with self.ledger.transaction():
                row = self.ledger.get("record", trace)
                row["rewritten"] = True
                self.ledger.put("record", trace, row)
        with self.ledger.transaction():
            row = self.ledger.get("record", trace)
            row["outbound_categories"] = list(hits)
            self.ledger.put("record", trace, row)
        if "privacy" in hits:
            raise ValueError("outbound_blocked")
        if hits:
            self.log.info("outbound.style", trace_id=trace)
        kind = "proactive" if proactive else "reply"
        groups = [[p] for p in draft.parts] if draft.form == "split" else [list(draft.parts)]
        groups = [g for g in groups if text_parts(g)]
        group = uuid4().hex
        intents = [
            self.intent(
                record,
                parts,
                kind=kind,
                target=draft.quote,
                split={"group_id": group, "index": i, "total": len(groups)}
                if len(groups) > 1
                else None,
            )
            for i, parts in enumerate(groups)
        ]
        for asset in assets:
            media = asset.media_type.split("/")[0]
            part = {
                "type": media if media in {"image", "audio", "video"} else "file",
                "asset": asset,
            }
            intents.append(self.intent(record, [part], kind=kind))
        if record["task_id"] and intents:
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
            card_hits = self.outbound.check(card, conversation.scene)
            if "privacy" in card_hits:
                self.log.warning("task_card.private_name", trace_id=trace)
                self.warnings.append("task_card.private_name: configure external name")
                if "task_card_private_name" not in self.boundary.warnings:
                    self.boundary.warnings.append("task_card_private_name")
                    self.boundary.save()
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
        await self.kernel.delivery.deliver(trace)

    async def submit(self, conversation_id, *, topic=None, material=None, parts=None, reason):
        if (
            self.shutting_down
            or self.kernel.delivery.silenced
            or self.model_health == "auth_invalid"
        ):
            trace = uuid4().hex
            with self.ledger.transaction():
                self.ledger.audit("proactive.dropped", trace_id=trace)
            return trace
        validate_proactive(topic=topic, material=material, parts=parts)
        target = self.instance.get("conversations", {}).get(conversation_id)
        if target is None or target["scene"] == "stranger_private":
            raise ValueError("proactive target denied")
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
        record = self.kernel.open_record(conversation=conversation)
        trace = record["trace_id"]
        with self.ledger.transaction():
            self.ledger.audit("proactive.submitted", trace_id=trace, reason=reason)
        self.log.info("proactive.submitted", trace_id=trace, reason=reason)
        extra = "这是你主动开口，不是在回复谁。话题和材料仅为内部说明：" + json.dumps(
            {"topic": topic, "material": material}, ensure_ascii=False
        )
        self.kernel.submit(
            trace,
            None,
            lambda: self.run(
                trace, conversation, None, [], extra=extra, proactive=True, direct=parts
            ),
        )
        return trace

    async def drain(self):
        await self.kernel.queue.drain()
        await self.kernel.reactions.drain()
