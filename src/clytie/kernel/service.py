"""测试显式装配的执行入口；默认拒绝权限，不装配真实平台或模型。"""

import asyncio
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime

from clytie.backup.coordination import run_sync, transaction
from clytie.plugin_host.support import HostLog
from clytie.sdk import CancelToken
from clytie.sdk.types import BusEvent, Denial, Invocation, TaskSummary, ToolResult

from .delivery import Delivery
from .events import EventBus, background
from .ledger import EXEC_TERMINAL, dump
from .projection import visible
from .query import record_timings
from .queue import ConversationQueue
from .reactions import Reactions

_current_trace = ContextVar("kernel_current_trace", default=None)


@contextmanager
def record_scope(trace):
    token = _current_trace.set(trace)
    try:
        yield
    finally:
        _current_trace.reset(token)


class Kernel:
    def __init__(
        self,
        ledger,
        clock,
        channels,
        *,
        config=None,
        max_task_seconds=3600,
        permissions=None,
        quota=None,
        observe=None,
        task_completed=None,
        log=None,
        event_bus=None,
    ):
        self.ledger, self.clock, self.channels = ledger, clock, channels
        self.config, self.max_task_seconds = config or {}, max_task_seconds
        self.permissions = permissions or (lambda actor, tool: False)
        self.quota = quota or (lambda actor, tool: False)
        self.observe, self.task_completed = observe, task_completed
        self.log = log or HostLog()
        self.subscribers, self.events, self.tokens, self.tasks = [], [], {}, {}
        self.bus = event_bus or EventBus(clock, self.log, lambda cid, e: self.subscribers)
        self.queue = ConversationQueue(
            clock, maximum=self.config.get("tasks", {}).get("max_concurrent", 2)
        )
        self.queued = {}
        self.grace = self.config.get("tasks", {}).get("cancel_grace_seconds", 5)
        self.delivery = Delivery(
            ledger,
            clock,
            channels,
            config=self.config.get("delivery"),
            grace=self.grace,
            on_sent=self._sent,
            on_complete=self.complete,
            log=self.log,
        )
        self.reactions = Reactions(ledger, self.delivery, clock, self.config.get("task_reactions"))
        self._reaction_exceptions = set()
        self.duplicates = 0
        self.conversation_scenes = {}
        self.record_tasks = {}

    async def start(self):
        interrupted = await run_sync(self.ledger, self.ledger.restart)
        await self.reactions.restart(interrupted)

    def submit(self, trace, actor, operation):
        record = self.ledger.get("record", trace)

        async def run():
            async with transaction(self.ledger):
                record = self.ledger.get("record", trace)
                if record["exec_status"] in EXEC_TERMINAL or record["cancelled"]:
                    return None
                record.update(exec_status="running", execution_started_at=self.ledger.now())
                self.ledger.put("record", trace, record)
            with record_scope(trace):
                return await operation()

        future = self.queue.submit(record["conversation_id"], actor, run)
        self.queued[trace] = future
        future.add_done_callback(lambda value: self.queued.pop(trace, None))
        return future

    def allow_reaction_exception(self, trace, intent, *, permissions=(), owner_ignore=False):
        record = self.ledger.get("record", trace)
        owner_exception = (
            owner_ignore
            and record["scene"] == "owner_private"
            and record["requester"]["role"] == "owner"
        )
        if "message.react" not in permissions and not owner_exception:
            self.log.warning("reaction.denied", trace_id=trace)
            return False
        self._reaction_exceptions.add(intent.intent_id)
        return True

    def emit(self, name, payload):
        event = BusEvent(name=name, payload=payload, at=self.clock.now(), source="core")
        self.events.append(event)
        self.bus.publish(self.ledger.companion_id, event)

    async def receive(self, event, *, filter_event):
        event = await filter_event(event)
        if event is None:
            return None
        if event.companion_id != self.ledger.companion_id:
            raise ValueError("event belongs to another companion")
        if not event.dedupable:
            if self.observe:
                await self.observe(event)
            return None
        event_id, new = await run_sync(self.ledger, self.ledger.claim_inbound, event)
        if not new:
            self.duplicates += 1
            self.log.debug("inbound.duplicate", event_id=event_id)
            return None
        if self.observe:
            await self.observe(event)
        return event_id

    def open_record(self, **kwargs):
        r = self.ledger.open_record(**kwargs)
        if r["source_event_id"]:
            self.emit(
                "message.received",
                dict(
                    event_id=r["source_event_id"],
                    trace_id=r["trace_id"],
                    conversation_id=r["conversation_id"],
                    scene=r["scene"],
                    actor_id=r["requester"]["actor_id"],
                ),
            )
        return r

    async def execute(
        self, trace, plugin_id, call, capability, invocation, *, schema=None, timeout_s=None
    ):
        if not self.permissions(invocation.actor, call.tool):
            return Denial(reason_code="permission", message_for_model="Permission denied")
        if not self.quota(invocation.actor, call.tool):
            return Denial(reason_code="quota", message_for_model="Quota insufficient")
        r = self.ledger.get("record", trace)
        key = capability.idempotency_key(call, invocation)
        complete_key = [
            self.ledger.companion_id,
            plugin_id,
            call.tool,
            r["source_event_id"] or trace,
            key,
        ]
        existing = self.ledger.find_invocation(complete_key)
        if existing:
            return self._existing(existing)
        plan = await capability.prepare(call, invocation)
        if isinstance(plan, Denial):
            return plan
        seconds = timeout_s or self.config.get("tasks", {}).get("default_deadline_seconds", 600)
        if seconds > self.max_task_seconds:
            self.log.warning("task.timeout_capped", tool=call.tool)
        inv, first = await run_sync(
            self.ledger,
            self.ledger.claim_invocation,
            trace,
            plugin_id,
            call,
            key,
            timeout=min(seconds, self.max_task_seconds),
            schema=schema or {},
        )
        async with transaction(self.ledger):
            inv["plan"] = {"summary": plan.summary}
            self.ledger.put("invocation", inv["invocation_id"], inv)
        if not await run_sync(self.ledger, self.ledger.start_invocation, inv["invocation_id"]):
            return self._existing(inv)
        r = self.ledger.get("record", trace)
        if first:
            self.emit(
                "task.started",
                dict(
                    task_id=r["task_id"],
                    trace_id=trace,
                    tool=r["tool"],
                    actor_id=invocation.actor.actor_id,
                    conversation_id=r["conversation_id"],
                ),
            )
            self.reactions.request(trace, "accepted")
        token = self.tokens.setdefault(trace, CancelToken())
        invocation = Invocation.model_validate({**dump(invocation), "task_id": r["task_id"]})
        if len([i for i in self.ledger.rows("invocation") if i["trace_id"] == trace]) == 1:
            self.reactions.request(trace, "running")
        with record_scope(trace):
            task = asyncio.create_task(capability.execute(plan, invocation, token.child()))
        self.tasks[trace] = task
        deadline = asyncio.create_task(
            self.clock.sleep_until(datetime.fromisoformat(r["deadline"]))
        )
        cancelled = asyncio.create_task(token.wait())
        settled = False
        task_cancelled = False

        async def cancel_invocation():
            nonlocal settled
            if settled:
                return None
            settled = True
            if not task.done():
                task.cancel()
            result = ToolResult(
                call_id=call.call_id,
                status="cancelled",
                content="",
                data={},
                error={"code": token.reason or "cancelled", "message": ""},
            )

            async def finalize():
                await run_sync(
                    self.ledger, self.ledger.finish_invocation, inv["invocation_id"], result
                )

                async def late():
                    try:
                        value = await task
                    except (Exception, asyncio.CancelledError):
                        return
                    await self.delivery.persist(
                        self.ledger.finish_invocation, inv["invocation_id"], value
                    )

                watcher = background(late())
                self.delivery.background.add(watcher)
                watcher.add_done_callback(self.delivery.background.discard)

            finalizer = background(finalize())
            self.delivery.background.add(finalizer)
            finalizer.add_done_callback(self.delivery.background.discard)
            await asyncio.shield(finalizer)
            return result

        try:
            done, _ = await asyncio.wait(
                {task, deadline, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
            if task not in done:
                if deadline in done:
                    token.cancel("deadline")
                grace = asyncio.create_task(self.clock.sleep(self.grace))
                try:
                    done, _ = await asyncio.wait({task, grace}, return_when=asyncio.FIRST_COMPLETED)
                    if task not in done:
                        return await cancel_invocation()
                finally:
                    grace.cancel()
                    await asyncio.gather(grace, return_exceptions=True)
            try:
                result = task.result()
            except asyncio.CancelledError:
                if not token.cancelled:
                    task_cancelled = True
                    raise
                result = ToolResult(
                    call_id=call.call_id,
                    status="cancelled",
                    content="",
                    data={},
                    error={"code": token.reason or "cancelled", "message": ""},
                )
            except Exception as exc:
                result = ToolResult(
                    call_id=call.call_id,
                    status="error",
                    content="",
                    data={},
                    error={"code": type(exc).__name__, "message": ""},
                )
            await self.delivery.persist(self.ledger.finish_invocation, inv["invocation_id"], result)
            settled = True
            return result
        except asyncio.CancelledError:
            # task.result() 的取消仍由原有分支处理；这里只收尾外部取消。
            if not task_cancelled:
                await cancel_invocation()
            raise
        finally:
            self.tasks.pop(trace, None)
            deadline.cancel()
            cancelled.cancel()
            try:
                await asyncio.gather(deadline, cancelled, return_exceptions=True)
            except asyncio.CancelledError:
                await cancel_invocation()
                raise

    def _existing(self, inv):
        r = self.ledger.get("record", inv["trace_id"])
        return dict(
            status="in_progress" if inv["status"] not in EXEC_TERMINAL else inv["status"],
            task_id=r["task_id"],
            result=inv["result"],
        )

    async def seal(self, trace, status, intents=(), error=None):
        if any(i.kind == "react" and i.intent_id not in self._reaction_exceptions for i in intents):
            raise ValueError("record cannot react without a declared exception")
        await run_sync(self.ledger, self.ledger.seal, trace, status, intents, error)
        await self.complete(trace)

    async def _sent(self, intent, receipt):
        self.emit(
            "reply.sent",
            dict(
                trace_id=intent.trace_id,
                conversation_id=intent.conversation_id,
                task_id=intent.task_id,
                intent_ids=[intent.intent_id],
            ),
        )

    async def complete(self, trace):
        async with transaction(self.ledger):
            r = self.ledger.get("record", trace)
            if (
                r["exec_status"] not in EXEC_TERMINAL
                or r["delivery_status"] == "pending"
                or r["notified"]
            ):
                return
            r["notified"] = True
            r["delivery_finished_at"] = self.ledger.now()
            self.ledger.put("record", trace, r)
        self.log.info(
            "record.completed",
            trace_id=trace,
            task_id=r["task_id"],
            status=r["exec_status"],
            tool=r["tool"],
            model_calls=r.get("model_calls", 0),
            tool_rounds=r.get("tool_rounds", 0),
            rewritten=r.get("rewritten", False),
            outbound_categories=r.get("outbound_categories", []),
            **record_timings(r),
        )
        if r["kind"] != "task":
            return
        invocations = [i for i in self.ledger.rows("invocation") if i["trace_id"] == trace]
        summary = TaskSummary(
            task_id=r["task_id"],
            trace_id=trace,
            tool=r["tool"],
            status="error" if r["exec_status"] == "interrupted" else r["exec_status"],
            actor_id=r["requester"]["actor_id"],
            conversation_id=r["conversation_id"],
            scene=r["scene"],
            started_at=r["started_at"] or r["claimed_at"],
            finished_at=r["finished_at"],
            asset_ids=list(dict.fromkeys(a for i in invocations for a in i["asset_ids"])),
            error_code=(r["error"] or {}).get("code"),
        )
        success = r["exec_status"] == "ok"
        self.emit("task.completed" if success else "task.failed", dump(summary))
        self.reactions.request(trace, "done" if success else "failed")
        if self.task_completed:
            await self.task_completed(summary)

    async def cancel(self, companion_id, actor, conversation_id, task_id=None, reply_to=None):
        if companion_id != self.ledger.companion_id or not conversation_id or actor is None:
            return "invalid_request"
        if (
            reply_to is None
            and task_id is not None
            and (not isinstance(task_id, str) or re.fullmatch(r"[0-9a-f]{8}", task_id) is None)
        ):
            return "invalid_request"
        records = [r for r in self.ledger.rows("record") if r["trace_id"] != _current_trace.get()]
        target = None
        if reply_to is not None:
            ref = dump(reply_to)
            events = {
                e["event_id"]
                for e in self.ledger.rows("inbound")
                if e.get("message") and e["message"]["ref"] == ref
            }
            traces = {r["trace_id"] for r in records if r["source_event_id"] in events}
            for intent in self.ledger.rows("intent"):
                if (
                    intent["channel_id"] != ref["channel_id"]
                    or intent["conversation_id"] != ref["conversation_id"]
                ):
                    continue
                if any(
                    a["intent_id"] == intent["intent_id"]
                    and a["receipt"]
                    and a["receipt"].get("platform_message_id") == ref["platform_message_id"]
                    for a in self.ledger.rows("attempt")
                ):
                    traces.add(intent["trace_id"])
            target = next((r for r in records if r["trace_id"] in traces), None)
        elif task_id:
            target = next((r for r in records if r["task_id"] == task_id), None)
        else:

            def own(r):
                return r["requester"] and r["requester"]["actor_id"] == actor.actor_id

            unfinished = [
                r
                for r in records
                if r["conversation_id"] == conversation_id
                and (r["exec_status"] not in EXEC_TERMINAL or r["delivery_status"] == "pending")
            ]
            tasks = [
                r for r in unfinished if r["kind"] == "task" and (actor.role == "owner" or own(r))
            ]
            queued = [r for r in unfinished if r["exec_status"] == "pending" and own(r)]
            target = (tasks or queued or [None])[-1]
        if target is None:
            return "not_found"
        current = self.ledger.get("record", _current_trace.get()) if _current_trace.get() else None
        scene = (
            current["scene"]
            if current and current["conversation_id"] == conversation_id
            else self.conversation_scenes.get(conversation_id, "public_group")
        )
        if not visible(
            target,
            scene=scene,
            conversation_id=conversation_id,
            private_scenes=self.config.get("memory", {}).get("private_scenes", ["owner_private"]),
        ):
            return "not_found"
        trace = target["trace_id"]
        allowed = (
            actor.role == "owner"
            or target["requester"]
            and target["requester"]["actor_id"] == actor.actor_id
        )
        async with transaction(self.ledger):
            target = self.ledger.get("record", trace)
            self.ledger.audit(
                "cancel_requested", trace_id=trace, actor_id=actor.actor_id, allowed=bool(allowed)
            )
            if not allowed:
                return "denied"
            if target["exec_status"] in EXEC_TERMINAL and target["delivery_status"] != "pending":
                return "already_finished"
            target["cancelled"] = True
            self.ledger.put("record", trace, target)
        token = self.tokens.setdefault(trace, CancelToken())
        token.cancel("cancelled")
        for intent in self.ledger.intents(trace):
            if not intent["status_reaction"] and not self.delivery.group_started(intent):
                code = (
                    intent["error"]["code"]
                    if intent["status"] == "retry_wait" and intent["error"]
                    else "cancelled_before_send"
                )
                await run_sync(self.ledger, self.ledger.fail_intent, intent["intent_id"], code)
                active = self.delivery.active_tokens.get(intent["intent_id"])
                if active:
                    active.cancel("cancelled")
        active_task = self.record_tasks.get(trace) or self.tasks.get(trace)
        if active_task is not None:
            timer = asyncio.create_task(self.clock.sleep(self.grace))
            try:
                done, _ = await asyncio.wait(
                    {active_task, timer}, return_when=asyncio.FIRST_COMPLETED
                )
                if active_task not in done:
                    active_task.cancel()
            finally:
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)
        if target["exec_status"] not in EXEC_TERMINAL:
            await run_sync(
                self.ledger,
                self.ledger.seal,
                trace,
                "cancelled",
                error={"code": "cancelled", "message": ""},
            )
        if target["exec_status"] == "pending" and trace in self.queued:
            self.queued[trace].cancel()
        await self.complete(trace)
        return "cancelled"
