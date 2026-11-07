"""持久化发送尝试之后才调用已注入的平台；unknown 永不重发。"""

import asyncio
from datetime import datetime

from clytie.backup.coordination import admission, run_sync
from clytie.sdk import CancelToken
from clytie.sdk.types import DeliveryIntent, Receipt

from .events import background
from .ledger import INTENT_TERMINAL


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

    async def persist(self, function, *args, **kwargs):
        """收据/执行结果一旦到达，取消调用者也不能丢弃等待提交的结果。"""
        if self.ledger.coordination is None:
            return function(*args, **kwargs)
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

    async def _call(self, intent, channel, token, cutoff, n):
        function = channel.react if intent.kind == "react" else channel.send
        task = asyncio.create_task(function(intent, token))
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
                        await self.persist(self.ledger.record_receipt, intent.intent_id, n, result)

                    watcher = background(collect_late())
                    self.background.add(watcher)
                    watcher.add_done_callback(self.background.discard)
                    return self._receipt(intent, "unknown", "receipt_timeout")
            try:
                return task.result()
            except (Exception, asyncio.CancelledError):
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

    async def send_one(self, intent_id):
        async with self._locks.setdefault(intent_id, asyncio.Lock()):
            return await self._send_one(intent_id)

    async def _send_one(self, intent_id):
        row = self.ledger.get("intent", intent_id)
        if row["status"] in INTENT_TERMINAL:
            return row["status"]
        intent = intent_value(row)
        channel = self.channels[intent.channel_id]
        if not self.asset_valid(intent, channel):
            await run_sync(self.ledger, self.ledger.fail_intent, intent_id, "asset_rejected")
            return "failed"
        group = (intent.trace_id, intent.split.group_id) if intent.split else None
        while True:
            row = self.ledger.get("intent", intent_id)
            if row["status"] in INTENT_TERMINAL:
                return row["status"]
            if (
                self.silenced
                or self.session_ending
                or self.channel_errors.get(intent.channel_id) == "auth_invalid"
            ):
                await run_sync(
                    self.ledger,
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
            if not row["status_reaction"] and not started and self._cancelled(row):
                await run_sync(
                    self.ledger,
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
                row = self.ledger.get("intent", intent_id)
                if row["status"] in INTENT_TERMINAL:
                    return row["status"]
            if started and self.clock.monotonic() >= self.groups[group][0]:
                await run_sync(self.ledger, self.ledger.fail_intent, intent_id, "group_timeout")
                return "failed"
            if not row["status_reaction"] and not started and self._cancelled(row):
                await run_sync(
                    self.ledger, self.ledger.fail_intent, intent_id, "cancelled_before_send"
                )
                return "failed"
            if group and not started:
                self.groups[group] = (self.clock.monotonic() + self.group_max, CancelToken())
            first = self.clock.monotonic()
            previous = [a for a in self.ledger.rows("attempt") if a["intent_id"] == intent_id]
            if previous:
                first -= (
                    self.clock.now() - datetime.fromisoformat(previous[0]["started_at"])
                ).total_seconds()
            cutoff, token = self.groups[group] if group else (first + self.window, CancelToken())
            if previous and self.clock.monotonic() >= first + self.window:
                await run_sync(
                    self.ledger, self.ledger.fail_intent, intent_id, row["error"]["code"]
                )
                return "failed"
            async with admission(self.ledger):
                row = self.ledger.get("intent", intent_id)
                if row["status"] in INTENT_TERMINAL:
                    return row["status"]
                if not row["status_reaction"] and not started and self._cancelled(row):
                    self.ledger.fail_intent(intent_id, "cancelled_before_send")
                    return "failed"
                if (
                    self.silenced
                    or self.session_ending
                    or self.channel_errors.get(intent.channel_id) == "auth_invalid"
                ):
                    await run_sync(
                        self.ledger,
                        self.ledger.fail_intent,
                        intent_id,
                        "silenced"
                        if self.silenced
                        else "shutdown_before_send"
                        if self.session_ending
                        else "auth_invalid",
                    )
                    return "failed"
                n = await run_sync(self.ledger, self.ledger.begin_attempt, intent_id)
            receipt = await self._call(intent, channel, token, cutoff, n)
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
                and (group or not self._cancelled(row))
            )
            fresh = await self.persist(
                self.ledger.record_receipt, intent_id, n, receipt, retry=bool(retry)
            )
            if (
                fresh
                and receipt.intent_id == intent_id
                and receipt.status == "sent"
                and self.on_sent
            ):
                await self.on_sent(intent, receipt)
            if not retry:
                return self.ledger.get("intent", intent_id)["status"]
            await self.clock.sleep(delay)

    async def deliver(self, trace):
        task = asyncio.current_task()
        self.workers.add(task)
        try:
            return await self._deliver(trace)
        finally:
            self.workers.discard(task)

    async def _deliver(self, trace):
        rows = [r for r in self.ledger.intents(trace) if not r["status_reaction"]]
        groups = {}
        for r in rows:
            key = r["split"]["group_id"] if r["split"] else r["intent_id"]
            groups.setdefault(key, []).append(r)
        for group in groups.values():
            group.sort(key=lambda r: r["split"]["index"] if r["split"] else 0)
            stopped = None
            for row in group:
                if stopped:
                    await run_sync(self.ledger, self.ledger.fail_intent, row["intent_id"], stopped)
                    continue
                status = await self.send_one(row["intent_id"])
                if status == "unknown":
                    stopped = "skipped_after_unknown"
                elif status == "failed":
                    error = self.ledger.get("intent", row["intent_id"])["error"]
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
