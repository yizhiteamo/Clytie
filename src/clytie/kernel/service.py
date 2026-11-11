"""测试显式装配的执行入口；默认拒绝权限，不装配真实平台或模型。"""

import asyncio
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime

from clytie.backup.coordination import run_sync
from clytie.plugin_host.support import HostLog
from clytie.sdk import CancelToken
from clytie.sdk.types import BusEvent, Denial, Invocation, TaskSummary, ToolResult

from .delivery import Delivery, OwnedFinalization
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
        self.finalizations = {}
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
        self.reactions.finalizations = self.finalizations
        self._reaction_exceptions = set()
        self.duplicates = 0
        self.conversation_scenes = {}
        self.record_tasks = {}

    async def start(self):
        interrupted = await run_sync(self.ledger, self.ledger.restart)
        await self.reactions.restart(interrupted)

    async def persist(self, trace, function, *args, **kwargs):
        finalization = self.finalizations.get(trace)
        if finalization is not None:
            return await finalization.persist(function, *args, **kwargs)
        return await run_sync(self.ledger, function, *args, **kwargs)

    async def submit(self, trace, actor, operation):
        record = await self.persist(trace, self.ledger.get, "record", trace)
        finalization = self.finalizations.get(trace)

        def begin():
            with self.ledger.transaction():
                record = self.ledger.get("record", trace)
                if record["exec_status"] in EXEC_TERMINAL or record["cancelled"]:
                    return False
                record.update(exec_status="running", execution_started_at=self.ledger.now())
                self.ledger.put("record", trace, record)
                return True

        async def run():
            if finalization is not None:
                finalization.track(asyncio.current_task())
            if not await self.persist(trace, begin):
                return None
            with record_scope(trace):
                return await operation()

        future = self.queue.submit(record["conversation_id"], actor, run)
        self.queued[trace] = future
        future.add_done_callback(lambda value: self.queued.pop(trace, None))
        return future

    async def allow_reaction_exception(self, trace, intent, *, permissions=(), owner_ignore=False):
        record = await self.persist(trace, self.ledger.get, "record", trace)
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

    async def receive(self, event, *, filter_event, finalization=None):
        event = await filter_event(event)
        if event is None:
            return None
        if event.companion_id != self.ledger.companion_id:
            raise ValueError("event belongs to another companion")
        if not event.dedupable:
            if self.observe:
                await self.observe(event)
            return None
        event_id, new = (
            await finalization.persist(self.ledger.claim_inbound, event)
            if finalization is not None
            else await run_sync(self.ledger, self.ledger.claim_inbound, event)
        )
        if not new:
            self.duplicates += 1
            self.log.debug("inbound.duplicate", event_id=event_id)
            return None
        if self.observe:
            await self.observe(event)
        return event_id

    async def open_record(self, *, finalization=None, **kwargs):
        r = (
            await finalization.persist(self.ledger.open_record, **kwargs)
            if finalization is not None
            else await run_sync(self.ledger, self.ledger.open_record, **kwargs)
        )
        if finalization is not None:
            self.finalizations[r["trace_id"]] = finalization
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
        self,
        trace,
        plugin_id,
        call,
        capability,
        invocation,
        *,
        schema=None,
        timeout_s=None,
        finalization=None,
    ):
        if not self.permissions(invocation.actor, call.tool):
            return Denial(reason_code="permission", message_for_model="Permission denied")
        if not self.quota(invocation.actor, call.tool):
            return Denial(reason_code="quota", message_for_model="Quota insufficient")
        finalization = finalization or self.finalizations.get(trace)
        operation = finalization or OwnedFinalization(self.delivery)
        state = {"cancelled": False, "executing": False, "task": None}

        def cancel():
            state["cancelled"] = True
            if state["executing"]:
                state["task"].cancel()
                return True
            return False

        follow = operation.follow if finalization is not None else operation.run
        return await follow(
            self._execute(
                trace,
                plugin_id,
                call,
                capability,
                invocation,
                finalization=operation,
                state=state,
                schema=schema,
                timeout_s=timeout_s,
            ),
            cancel=cancel,
        )

    async def _execute(
        self,
        trace,
        plugin_id,
        call,
        capability,
        invocation,
        *,
        finalization,
        state,
        schema=None,
        timeout_s=None,
    ):
        r = await finalization.persist(self.ledger.get, "record", trace)
        key = capability.idempotency_key(call, invocation)
        complete_key = [
            self.ledger.companion_id,
            plugin_id,
            call.tool,
            r["source_event_id"] or trace,
            key,
        ]
        existing = await finalization.persist(self.ledger.find_invocation, complete_key)
        if existing:
            return await finalization.persist(self._existing, existing)
        plan = await capability.prepare(call, invocation)
        if isinstance(plan, Denial):
            return plan
        if state["cancelled"]:
            return ToolResult(
                call_id=call.call_id,
                status="cancelled",
                content="",
                data={},
                error={"code": "cancelled", "message": ""},
            )
        seconds = timeout_s or self.config.get("tasks", {}).get("default_deadline_seconds", 600)
        if seconds > self.max_task_seconds:
            self.log.warning("task.timeout_capped", tool=call.tool)
        inv, first = await finalization.persist(
            self.ledger.claim_invocation,
            trace,
            plugin_id,
            call,
            key,
            timeout=min(seconds, self.max_task_seconds),
            schema=schema or {},
        )

        def store_plan():
            with self.ledger.transaction():
                current = self.ledger.get("invocation", inv["invocation_id"])
                current["plan"] = {"summary": plan.summary}
                self.ledger.put("invocation", inv["invocation_id"], current)

        await finalization.persist(store_plan)
        if not await finalization.persist(self.ledger.start_invocation, inv["invocation_id"]):
            return await finalization.persist(self._existing, inv)
        r = await finalization.persist(self.ledger.get, "record", trace)
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
            await self.reactions.request(trace, "accepted", finalization=finalization)
        token = self.tokens.setdefault(trace, CancelToken())
        invocation = Invocation.model_validate({**dump(invocation), "task_id": r["task_id"]})
        invocations = await finalization.persist(self.ledger.rows, "invocation")
        if len([i for i in invocations if i["trace_id"] == trace]) == 1:
            await self.reactions.request(trace, "running", finalization=finalization)
        if state["cancelled"]:
            result = ToolResult(
                call_id=call.call_id,
                status="cancelled",
                content="",
                data={},
                error={"code": "cancelled", "message": ""},
            )
            await finalization.persist(self.ledger.finish_invocation, inv["invocation_id"], result)
            return result
        state["task"] = asyncio.current_task()
        state["executing"] = True
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
                await finalization.persist(
                    self.ledger.finish_invocation, inv["invocation_id"], result
                )

                async def late():
                    try:
                        value = await task
                    except (Exception, asyncio.CancelledError):
                        return
                    await finalization.persist(
                        self.ledger.finish_invocation, inv["invocation_id"], value
                    )

                finalization.track(background(late()))

            finalizer = finalization.track(background(finalize()))
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
                    await cancel_invocation()
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
            settled = True
            await finalization.persist(self.ledger.finish_invocation, inv["invocation_id"], result)
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
        await self.persist(trace, self.ledger.seal, trace, status, intents, error)
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

    def _complete_record(self, trace):
        with self.ledger.transaction():
            r = self.ledger.get("record", trace)
            if (
                r["exec_status"] not in EXEC_TERMINAL
                or r["delivery_status"] == "pending"
                or r["notified"]
            ):
                return None
            r["notified"] = True
            r["delivery_finished_at"] = self.ledger.now()
            self.ledger.put("record", trace, r)
            return r

    async def complete(self, trace):
        r = await self.persist(trace, self._complete_record, trace)
        if r is None:
            return
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
        invocations = [
            i
            for i in await self.persist(trace, self.ledger.rows, "invocation")
            if i["trace_id"] == trace
        ]
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
        await self.reactions.request(trace, "done" if success else "failed")
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
        target = await run_sync(
            self.ledger,
            self._cancel_target,
            actor,
            conversation_id,
            task_id,
            reply_to,
            _current_trace.get(),
            self.conversation_scenes.get(conversation_id, "public_group"),
        )
        if isinstance(target, str):
            return target
        trace = target["trace_id"]
        token = self.tokens.setdefault(trace, CancelToken())
        token.cancel("cancelled")
        for intent in await self.persist(trace, self.ledger.intents, trace):
            if not intent["status_reaction"] and not self.delivery.group_started(intent):
                code = (
                    intent["error"]["code"]
                    if intent["status"] == "retry_wait" and intent["error"]
                    else "cancelled_before_send"
                )
                await self.persist(trace, self.ledger.fail_intent, intent["intent_id"], code)
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
            await self.persist(
                trace,
                self.ledger.seal,
                trace,
                "cancelled",
                error={"code": "cancelled", "message": ""},
            )
        if target["exec_status"] == "pending" and trace in self.queued:
            self.queued[trace].cancel()
        await self.complete(trace)
        return "cancelled"

    def _cancel_target(self, actor, conversation_id, task_id, reply_to, current_trace, scene):
        with self.ledger.transaction():
            return self._cancel_target_in_transaction(
                actor, conversation_id, task_id, reply_to, current_trace, scene
            )

    def _cancel_target_in_transaction(
        self, actor, conversation_id, task_id, reply_to, current_trace, scene
    ):
        records = [r for r in self.ledger.rows("record") if r["trace_id"] != current_trace]
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
        current = self.ledger.get("record", current_trace) if current_trace else None
        scene = (
            current["scene"] if current and current["conversation_id"] == conversation_id else scene
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
        return target
