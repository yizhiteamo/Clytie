"""单个平台连接的历史读取、恢复窗口和私聊提醒。"""

import asyncio
import json
import re
from datetime import datetime, timedelta

from clytie.agent.catch_up import HeldConversation
from clytie.backup.coordination import run_sync
from clytie.sdk import CancelToken
from clytie.sdk.channel import channel_state
from clytie.sdk.types import EventEnvelope

DEFAULTS = dict(
    settle_seconds=10,
    min_interval_seconds=60,
    overlap_minutes=5,
    request_interval_seconds=1,
    conversation_timeout_seconds=30,
    private_hours=24,
    private_max=50,
    group_hours=24,
    group_scan_max=200,
    group_people_max=3,
    group_addressed_max=10,
    group_context_before=5,
    failed_replies=True,
    group_stale_minutes=60,
    notice_after_minutes=15,
    notice_interval_hours=3,
)


async def finish_accepted(operation):
    """取消后等待已认领入站交回责任，避免关停时遗留收尾凭证。"""
    task = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class PlatformCatchUp:
    def __init__(self, companion, binding, plugin, channel):
        self.companion = companion
        self.agent = companion.agent
        self.clock = companion.clock
        self.ledger = self.agent.ledger
        self.binding = binding
        self.channel = channel
        self.settings = DEFAULTS | self.agent.config.get("catch_up", {})
        admission = self.agent.admission
        self.targets = {}
        for cid, conv in admission.conversations.items():
            if (
                conv["channel"] == plugin
                and (actor := admission.private_actors.get(cid))
                and actor.role == "owner"
            ):
                self.targets[cid] = True
        for cid, conv in admission.conversations.items():
            if conv["channel"] == plugin and conv["kind"] == "group":
                self.targets[cid] = False
        self.online = False
        self.down_since = self.last_notice = self.notice_recovery = None
        self.last_completed, self.watermarks = {}, {}
        self.pending_recoveries = {}
        self.anchor_lock = asyncio.Lock()
        self.failed = 0
        self.holds = {}
        self.failed_lookup = None
        self.cycle = None
        self.jobs = set()

    def track(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.jobs.add(task)

        def completed(task):
            self.jobs.discard(task)
            if not task.cancelled() and (exc := task.exception()) is not None:
                self.failed += 1
                self.companion.log.warning(
                    "catch_up.failed", error_type=type(exc).__name__, code="history_failed"
                )

        task.add_done_callback(completed)
        return task

    def observe(self):
        online = channel_state(self.channel).status == "online"
        if self.online == online or self.agent.shutting_down or self.companion.stop_requested():
            return
        self.online = online
        now = self.clock.monotonic()
        if not online:
            self.down_since = now
            if self.cycle:
                self.cycle.cancel()
            holds, self.holds = self.holds, {}
            for held in holds.values():
                if held.waiting:
                    self.track(finish_accepted(held.release_live()))
            return
        downtime = None if self.down_since is None else now - self.down_since
        self.down_since = None
        supported = self.channel.features().supports_history
        holds = {
            cid: HeldConversation(self.agent, private) for cid, private in self.targets.items()
        }
        restored = self.clock.now()
        self.failed_lookup = (
            self.track(run_sync(self.ledger, self.failed_replies, restored))
            if not supported and self.settings["failed_replies"]
            else None
        )
        self.holds = holds
        anchors = None
        if supported:
            # A short-lived connection still establishes the earliest boundary.
            # Finishing this local lookup is independent of reading the platform.
            anchors = self.track(self.remember_anchors(restored, self.pending_recoveries))
        else:
            self.pending_recoveries = {}
        self.cycle = self.track(self.run(restored, downtime, holds, anchors))

    async def holding_for(self, cid):
        held = self.holds.get(cid)
        if held and not self.channel.features().supports_history:
            lookup = self.failed_lookup
            if lookup is None or cid not in await asyncio.shield(lookup):
                return None
            if held is not self.holds.get(cid):
                return None
        return held

    def failed_replies(self, restored):
        """Read eligible records and their merged inputs on the ledger owner."""
        result = {}
        for cid, private in self.targets.items():
            hours = self.settings["private_hours" if private else "group_hours"]
            since = restored - timedelta(hours=hours)
            for trace, value in self.ledger.failed_reply_inputs(self.binding, cid, since, restored):
                if not (value.get("message") or {}).get("parts"):
                    continue
                event = self.agent.conversation_messages.mark(
                    EventEnvelope.model_validate(
                        {key: value[key] for key in EventEnvelope.model_fields if key in value}
                    )
                )
                flags = event.addressed
                if (
                    event.channel_id == self.binding
                    and event.conversation.conversation_id == cid
                    and (
                        event.message.sender.role == "owner"
                        if private
                        else flags.mention or flags.reply_to_self or flags.name
                    )
                ):
                    result.setdefault(cid, []).append((event, trace))
        return result

    async def watch(self):
        while True:
            self.observe()
            await self.clock.sleep(0.25)

    def anchors(self, cutoffs):
        # SDK timestamps are UTC. Comparing without the suffix preserves
        # microseconds, including when one timestamp is exactly a whole second.
        rows = self.ledger.db.execute(
            """
            WITH cutoffs AS (SELECT key AS cid, value AS cutoff FROM json_each(?)),
            candidates AS (
                SELECT rowid AS seq, json_extract(data,'$.conversation_id') AS cid,
                       json_extract(data,'$.message.sent_at') AS sent,
                       rtrim(replace(json_extract(data,'$.received_at'),'+00:00',''),'Z') AS received
                  FROM inbound WHERE json_extract(data,'$.channel_id')=?
            ), ranked AS (
                SELECT cid, sent,
                       row_number() OVER (PARTITION BY cid ORDER BY received DESC, seq DESC) AS rank
                  FROM candidates JOIN cutoffs USING (cid)
                 WHERE received < cutoff AND sent IS NOT NULL
            )
            SELECT cid, sent FROM ranked WHERE rank=1
            """,
            (json.dumps(cutoffs), self.binding),
        )
        return dict(rows)

    async def remember_anchors(self, restored, pending):
        async with self.anchor_lock:
            cutoffs = {
                cid: pending.get(cid, restored).isoformat().removesuffix("+00:00")
                for cid in self.targets
            }
            anchors = await run_sync(self.ledger, self.anchors, cutoffs)
            for cid in self.targets:
                if cid in anchors:
                    pending.setdefault(cid, restored)
                else:
                    pending.pop(cid, None)
            return anchors

    def anchor(self, cid, sent):
        hours = self.settings["private_hours" if self.targets[cid] else "group_hours"]
        start = datetime.fromisoformat(sent.replace("Z", "+00:00")) - timedelta(
            minutes=self.settings["overlap_minutes"]
        )
        return max(
            start, self.clock.now() - timedelta(hours=hours), self.watermarks.get(cid, start)
        )

    async def audit(self, kind, **fields):
        def write():
            with self.ledger.transaction():
                self.ledger.audit(kind, **fields)

        await run_sync(self.ledger, write)

    async def read(self, cid, after, limit):
        cancel = CancelToken()
        job = asyncio.create_task(
            self.channel.history(cid, after=after, limit=limit, cancel=cancel)
        )
        timer = asyncio.create_task(self.clock.sleep(self.settings["conversation_timeout_seconds"]))
        try:
            done, _ = await asyncio.wait((job, timer), return_when=asyncio.FIRST_COMPLETED)
            if job in done:
                return await job
            cancel.cancel("timeout")
            # Give cooperative implementations their accumulated page, then also
            # interrupt a transport which is blocked waiting for a reply.
            await self.clock.sleep(0)
            if not job.done():
                job.cancel()
            try:
                rows = await job
            except asyncio.CancelledError:
                if not job.cancelled():
                    raise
                rows = []
            if not rows:
                raise TimeoutError
            return rows
        finally:
            cancel.cancel("stopped")
            job.cancel()
            timer.cancel()
            await asyncio.gather(job, timer, return_exceptions=True)

    async def notice(self, cid, restored, downtime):
        if (
            not self.targets[cid]
            or self.notice_recovery == restored
            or downtime is None
            or downtime < self.settings["notice_after_minutes"] * 60
        ):
            return
        now = self.clock.monotonic()
        if (
            self.last_notice is not None
            and now - self.last_notice < self.settings["notice_interval_hours"] * 3600
        ):
            return
        result = await self.agent.submit(
            cid,
            reason="catch_up",
            tools=False,
            topic=f"刚才大约有 {int(downtime // 60)} 分钟不在线，期间主人的消息可能没收到。"
            "按你平时的口吻告诉主人，可以请主人有事再说一遍。不要用固定的说法，不要编造你当时在做什么。",
        )
        if result.status == "accepted":
            self.last_notice = now
            self.notice_recovery = restored
            await self.audit("catch_up.notice", conversation_id=cid)

    async def one(self, cid, held, restored, downtime, anchors, retries):
        async def without_history():
            if retries:
                await self.process(cid, held, [], False, retries)
            else:
                await finish_accepted(held.release_live())

        if not self.channel.features().supports_history:
            self.pending_recoveries = {}
            await without_history()
            await self.notice(cid, restored, downtime)
            return
        if cid not in anchors:
            await without_history()
            return
        completed = self.last_completed.get(cid)
        if (
            completed is not None
            and self.clock.monotonic() - completed < self.settings["min_interval_seconds"]
        ):
            await without_history()
            return
        after = self.anchor(cid, anchors[cid])
        limit = self.settings["private_max" if held.private else "group_scan_max"]
        try:
            rows = await self.read(cid, after, limit)
        except Exception as exc:
            if not self.channel.features().supports_history:
                self.pending_recoveries = {}
            code = getattr(exc, "code", None)
            if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z_]{0,63}", code):
                code = "timeout" if isinstance(exc, TimeoutError) else "history_failed"
            self.failed += 1
            await self.audit(
                "catch_up.failed", conversation_id=cid, error_type=type(exc).__name__, code=code
            )
            await without_history()
            await self.notice(cid, restored, downtime)
            return
        rows = sorted(
            (
                e
                for e in rows
                if e.type == "conversation.message"
                and e.message
                and e.companion_id == self.agent.cid
                and e.channel_id == self.binding
                and e.conversation.conversation_id == cid
                and e.message.sent_at > after
                and (not held.private or e.message.sender.role == "owner")
            ),
            key=lambda e: e.message.sent_at,
        )[-limit:]
        await self.process(cid, held, rows, len(rows) == limit, retries)
        if channel_state(self.channel).status != "online":
            return
        if rows:
            self.watermarks[cid] = rows[-1].message.sent_at
        self.last_completed[cid] = self.clock.monotonic()
        self.pending_recoveries.pop(cid, None)
        if not self.channel.features().supports_history:
            self.pending_recoveries = {}

    async def process(self, cid, held, rows, full, retries):
        async def ingest():
            for event in rows:
                if self.agent.shutting_down:
                    break
                await self.companion._receive_event(event, holding=held, historical=True)
            await held.stage_retries(retries)
            return await held.finish(self.settings, full, tuple(e.event_id for e in rows))

        new, responding, recorded = await finish_accepted(ingest())
        if channel_state(self.channel).status != "online":
            return
        await self.audit(
            "catch_up.done",
            conversation_id=cid,
            read=len(rows),
            unseen=new,
            responding=responding,
            recorded=recorded,
            limit_reached=full,
            failed_replies=len(held.retry_traces),
            stale_judged=held.judged,
            stale_respond=held.judged_respond,
        )

    async def run(self, restored, downtime, holds, anchor_lookup):
        async def release_at_deadline():
            await self.clock.sleep(
                self.settings["settle_seconds"] + self.settings["conversation_timeout_seconds"]
            )
            await finish_accepted(asyncio.gather(*(held.release_live() for held in holds.values())))

        deadline = asyncio.create_task(release_at_deadline())
        try:
            await self.clock.sleep(self.settings["settle_seconds"])
            anchors = await asyncio.shield(anchor_lookup) if anchor_lookup is not None else {}
            # Delivery may finish during the settling interval. Eligibility is
            # evaluated now, using the original connection time as the cutoff.
            retries = (
                await run_sync(self.ledger, self.failed_replies, restored)
                if self.settings["failed_replies"]
                else {}
            )
            for index, (cid, held) in enumerate(holds.items()):
                if index:
                    await self.clock.sleep(self.settings["request_interval_seconds"])
                if not self.online or self.agent.shutting_down or self.companion.stop_requested():
                    break
                await self.one(cid, held, restored, downtime, anchors, retries.get(cid, []))
        finally:
            deadline.cancel()
            await asyncio.gather(deadline, return_exceptions=True)
            for held in holds.values():
                await held.finish(self.settings, False)
            if self.holds is holds:
                self.holds = {}

    async def stop(self):
        tasks = list(self.jobs)
        for job in tasks:
            job.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for held in self.holds.values():
            await held.finish(self.settings, False)
        self.holds = {}
