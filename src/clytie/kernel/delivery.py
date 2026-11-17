"""持久化发送尝试之后才调用已注入的平台；unknown 永不重发。"""

import asyncio
from datetime import datetime

from clytie.backup.coordination import run_sync
from clytie.sdk import CancelToken
from clytie.sdk.types import DeliveryIntent, Receipt
from clytie.storage.finalization import finalizer_scope, release_finalizer, reserve_finalizer

from .events import background
from .ledger import INTENT_TERMINAL


class OwnedFinalization:
    """一个执行器及其迟到结果共享收尾容量，持久命令逐个完成。"""

    def __init__(self, delivery):
        self.delivery = delivery
        self.token = reserve_finalizer(delivery.ledger)
        self.tasks = set()
        self.lock = asyncio.Lock()
        self.failed = False
        self.leases = 1
        self.released = False
        self._completion_callbacks = []

    def on_complete(self, callback):
        if self.released:
            callback()
        else:
            self._completion_callbacks.append(callback)

    def release(self):
        """交回根责任；已开始的提交和迟到执行器继续持有凭证。"""
        if self.leases:
            self.leases -= 1
        self._release_if_complete()

    def _release_if_complete(self):
        if not self.released and not self.leases and not self.tasks and not self.failed:
            release_finalizer(self.token, executor_finished=True, obligations_complete=True)
            self.released = True
            for callback in self._completion_callbacks:
                callback()
            self._completion_callbacks.clear()

    def track(self, task):
        if task in self.tasks:
            return task
        self.tasks.add(task)
        self.delivery.background.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task):
        self.tasks.discard(task)
        self.delivery.background.discard(task)
        if not task.cancelled():
            task.exception()  # 后台失败仍由原等待者传播，不产生未领取异常。
        self._release_if_complete()

    async def persist(self, function, *args, **kwargs):
        async def command():
            async with self.lock:
                try:
                    with finalizer_scope(self.token):
                        return await run_sync(self.delivery.ledger, function, *args, **kwargs)
                except BaseException:
                    self.failed = True
                    raise

        return await asyncio.shield(self.track(asyncio.create_task(command())))

    async def run(self, coroutine, *, cancel=None):
        try:
            return await self.follow(coroutine, cancel=cancel)
        finally:
            self.release()

    async def follow(self, coroutine, *, cancel=None):
        """子执行器借用同一责任；其结束不交回父记录的根 lease。"""
        task = self.track(asyncio.create_task(coroutine))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if cancel is None:
                task.cancel()
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass
            else:
                if cancel():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        pass
            raise


def intent_value(row):
    return DeliveryIntent.model_validate(
        {k: v for k, v in row.items() if k in DeliveryIntent.model_fields}
    )


class Delivery:
    def __init__(
        self,
        ledger,
        clock,
        channels,
        *,
        config=None,
        grace=5,
        on_sent=None,
        on_complete=None,
        log=None,
    ):
        self.ledger, self.clock, self.channels = ledger, clock, channels
        self.config = config or {}
        retry = self.config.get("retry", {})
        self.max_attempts = retry.get("max_attempts", 3)
        self.backoff = retry.get("backoff_seconds", [2, 5])
        self.window = retry.get("window_seconds", 60)
        self.group_max = self.config.get("group_max_seconds", 60)
        self.grace = grace
        self.on_sent, self.on_complete = on_sent, on_complete
        self.groups, self.background = {}, set()
        self._locks = {}
        self.active_tokens = {}
        self.log = log
        self.silenced = False
        self.session_ending = False
        self.channel_errors = {}
        self.workers = set()
        self.admission = None

    async def persist(self, function, *args, **kwargs):
        """收据/执行结果一旦到达，取消调用者也不能丢弃等待提交的结果。"""
        task = background(run_sync(self.ledger, function, *args, **kwargs))
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return await asyncio.shield(task)

    def _receipt(self, intent, status, code):
        return Receipt(
            intent_id=intent.intent_id,
            status=status,
            at=self.clock.now(),
            error={"code": code, "message": ""},
        )

    def group_started(self, row):
        split = row.get("split")
        return split and (row["trace_id"], split["group_id"]) in self.groups

    def asset_valid(self, intent, channel):
        limits = channel.features().media_limits
        for part in intent.parts:
            if part.type not in {"image", "audio", "video", "file"}:
                continue
            limit = limits.get(part.type)
            if not limit or part.asset is None:
                return False
            if (
                part.asset.size > limit["max_bytes"]
                or part.asset.media_type not in limit["formats"]
            ):
                return False
        return True

    async def _call(self, intent, channel, token, cutoff, n, finalization, receipt_recorded):
        function = channel.react if intent.kind == "react" else channel.send
        admission_pending = self.admission is not None

        async def invoke():
            nonlocal admission_pending
            blocked = self._blocked(intent.channel_id)
            if blocked:
                return self._receipt(intent, "failed", blocked)
            if self.admission is not None:
                record = await finalization.persist(self.ledger.get, "record", intent.trace_id)
                inbound = (
                    await finalization.persist(
                        self.ledger.get, "inbound", record["source_event_id"]
                    )
                    if record.get("source_event_id")
                    else None
                )
                if (
                    not await self.admission.allows_delivery_checked(intent, inbound)
                    or token.cancelled
                ):
                    return self._receipt(intent, "failed", "not_admitted")
            admission_pending = False
            return await function(intent, token)

        task = asyncio.create_task(invoke())
        timer = asyncio.create_task(self.clock.sleep(max(0, cutoff - self.clock.monotonic())))
        cancelled = asyncio.create_task(token.wait())
        self.active_tokens[intent.intent_id] = token
        try:
            done, _ = await asyncio.wait(
                {task, timer, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
            if task not in done:
                token.cancel("deadline")
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)
                timer = asyncio.create_task(self.clock.sleep(self.grace))
                done, _ = await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
                if task not in done:

                    async def collect_late():
                        try:
                            result = await task
                        except (Exception, asyncio.CancelledError):
                            return
                        await receipt_recorded.wait()
                        await finalization.persist(
                            self.ledger.record_receipt, intent.intent_id, n, result
                        )

                    finalization.track(background(collect_late()))
                    if admission_pending:
                        return self._receipt(intent, "failed", "not_admitted")
                    return self._receipt(intent, "unknown", "receipt_timeout")
            try:
                return task.result()
            except (Exception, asyncio.CancelledError):
                if admission_pending:
                    return self._receipt(intent, "failed", "not_admitted")
                return self._receipt(intent, "unknown", "channel_exception")
        finally:
            self.active_tokens.pop(intent.intent_id, None)
            timer.cancel()
            cancelled.cancel()
            await asyncio.gather(timer, cancelled, return_exceptions=True)

    def _cancelled(self, row):
        r = self.ledger.get("record", row["trace_id"])
        return r["cancelled"] or bool(
            r["deadline"] and self.clock.now() >= datetime.fromisoformat(r["deadline"])
        )

    async def send_one(self, intent_id, *, finalization=None):
        async def send():
            async with self._locks.setdefault(intent_id, asyncio.Lock()):
                return await self._send_one(intent_id, operation)

        operation = finalization or OwnedFinalization(self)

        def cancel():
            token = self.active_tokens.get(intent_id)
            if token is not None:
                token.cancel("cancelled")

        follow = operation.follow if finalization is not None else operation.run
        return await follow(send(), cancel=cancel)

    async def _send_one(self, intent_id, finalization):
        row = await finalization.persist(self.ledger.get, "intent", intent_id)
        if row["status"] in INTENT_TERMINAL:
            return row["status"]
        intent = intent_value(row)
        channel = self.channels[intent.channel_id]
        if not self.asset_valid(intent, channel):
            await finalization.persist(self.ledger.fail_intent, intent_id, "asset_rejected")
            return "failed"
        group = (intent.trace_id, intent.split.group_id) if intent.split else None
        while True:
            row = await finalization.persist(self.ledger.get, "intent", intent_id)
            if row["status"] in INTENT_TERMINAL:
                return row["status"]
            if (
                self.silenced
                or self.session_ending
                or self.channel_errors.get(intent.channel_id) == "auth_invalid"
            ):
                await finalization.persist(
                    self.ledger.fail_intent,
                    intent_id,
                    "silenced"
                    if self.silenced
                    else "shutdown_before_send"
                    if self.session_ending
                    else "auth_invalid",
                )
                return "failed"
            started = group in self.groups if group else False
            if (
                not row["status_reaction"]
                and not started
                and await finalization.persist(self._cancelled, row)
            ):
                await finalization.persist(
                    self.ledger.fail_intent,
                    intent_id,
                    (row["error"] or {}).get("code", "cancelled_before_send"),
                )
                return "failed"
            if intent.not_before and self.clock.now() < intent.not_before:
                seconds = (intent.not_before - self.clock.now()).total_seconds()
                if started:
                    seconds = min(seconds, max(0, self.groups[group][0] - self.clock.monotonic()))
                await self.clock.sleep(seconds)
                row = await finalization.persist(self.ledger.get, "intent", intent_id)
                if row["status"] in INTENT_TERMINAL:
                    return row["status"]
            if started and self.clock.monotonic() >= self.groups[group][0]:
                await finalization.persist(self.ledger.fail_intent, intent_id, "group_timeout")
                return "failed"
            if (
                not row["status_reaction"]
                and not started
                and await finalization.persist(self._cancelled, row)
            ):
                await finalization.persist(
                    self.ledger.fail_intent, intent_id, "cancelled_before_send"
                )
                return "failed"
            if group and not started:
                self.groups[group] = (self.clock.monotonic() + self.group_max, CancelToken())
            first = self.clock.monotonic()
            previous = [
                a
                for a in await finalization.persist(self.ledger.rows, "attempt")
                if a["intent_id"] == intent_id
            ]
            if previous:
                first -= (
                    self.clock.now() - datetime.fromisoformat(previous[0]["started_at"])
                ).total_seconds()
            cutoff, token = self.groups[group] if group else (first + self.window, CancelToken())
            if previous and self.clock.monotonic() >= first + self.window:
                await finalization.persist(self.ledger.fail_intent, intent_id, row["error"]["code"])
                return "failed"
            status, n = await finalization.persist(self._begin_attempt, intent_id, started)
            if status is not None:
                return status
            receipt_recorded = asyncio.Event()
            blocked = self._blocked(intent.channel_id)
            if blocked:
                receipt = self._receipt(intent, "failed", blocked)
            else:
                receipt = await self._call(
                    intent, channel, token, cutoff, n, finalization, receipt_recorded
                )
            if receipt.intent_id != intent_id and self.log:
                self.log.warning("delivery.receipt_mismatch", intent_id=intent_id, attempt=n)
            if receipt.error and receipt.error.code in {"auth_invalid", "quota_exhausted"}:
                self.channel_errors[intent.channel_id] = receipt.error.code
            delay = self.backoff[min(n - 1, len(self.backoff) - 1)] if self.backoff else 0
            retry = (
                not row["status_reaction"]
                and receipt.intent_id == intent_id
                and receipt.status == "failed"
                and receipt.error
                and receipt.error.code in {"transient", "rate_limited"}
                and n < self.max_attempts
                and self.clock.monotonic() + delay < first + self.window
                and self.clock.monotonic() + delay < cutoff
                and (group or not await finalization.persist(self._cancelled, row))
            )
            fresh = await finalization.persist(
                self.ledger.record_receipt, intent_id, n, receipt, retry=bool(retry)
            )
            receipt_recorded.set()
            if (
                fresh
                and receipt.intent_id == intent_id
                and receipt.status == "sent"
                and self.on_sent
            ):
                await self.on_sent(intent, receipt)
            if not retry:
                return (await finalization.persist(self.ledger.get, "intent", intent_id))["status"]
            await self.clock.sleep(delay)

    async def deliver(self, trace, *, finalization=None):
        operation = finalization or OwnedFinalization(self)
        follow = operation.follow if finalization is not None else operation.run
        return await follow(self._owned_deliver(trace, operation), cancel=lambda: None)

    async def _owned_deliver(self, trace, finalization):
        task = asyncio.current_task()
        self.workers.add(task)
        try:
            return await self._deliver(trace, finalization)
        finally:
            self.workers.discard(task)

    async def _deliver(self, trace, finalization):
        rows = [
            r
            for r in await finalization.persist(self.ledger.intents, trace)
            if not r["status_reaction"]
        ]
        groups = {}
        for r in rows:
            key = r["split"]["group_id"] if r["split"] else r["intent_id"]
            groups.setdefault(key, []).append(r)
        for group in groups.values():
            group.sort(key=lambda r: r["split"]["index"] if r["split"] else 0)
            stopped = None
            for row in group:
                if stopped:
                    await finalization.persist(self.ledger.fail_intent, row["intent_id"], stopped)
                    continue
                status = await self.send_one(row["intent_id"], finalization=finalization)
                if status == "unknown":
                    stopped = "skipped_after_unknown"
                elif status == "failed":
                    error = (
                        await finalization.persist(self.ledger.get, "intent", row["intent_id"])
                    )["error"]
                    stopped = (
                        error["code"]
                        if error
                        and error["code"]
                        in {
                            "group_timeout",
                            "cancelled_before_send",
                            "silenced",
                            "shutdown_before_send",
                            "auth_invalid",
                        }
                        else "skipped_after_failure"
                    )
        if self.on_complete:
            await self.on_complete(trace)

    def _blocked(self, channel_id):
        return (
            "silenced"
            if self.silenced
            else "shutdown_before_send"
            if self.session_ending
            else "auth_invalid"
            if self.channel_errors.get(channel_id) == "auth_invalid"
            else None
        )

    def _begin_attempt(self, intent_id, started):
        row = self.ledger.get("intent", intent_id)
        if row["status"] in INTENT_TERMINAL:
            return row["status"], None
        if not row["status_reaction"] and not started and self._cancelled(row):
            self.ledger.fail_intent(intent_id, "cancelled_before_send")
            return "failed", None
        blocked = self._blocked(row["channel_id"])
        if blocked:
            self.ledger.fail_intent(intent_id, blocked)
            return "failed", None
        return None, self.ledger.begin_attempt(intent_id)
