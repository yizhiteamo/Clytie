"""任务状态回应；独立于正文交付汇总，失败不重试、不转文字。"""

import asyncio
from uuid import uuid4

from clytie.backup.coordination import run_sync
from clytie.sdk.types import DeliveryIntent

from .delivery import OwnedFinalization
from .events import background


class Reactions:
    def __init__(self, ledger, delivery, clock, config=None):
        self.ledger, self.delivery, self.clock = ledger, delivery, clock
        config = config or {}
        self.enabled, self.audience = config.get("enabled", True), config.get("audience", "all")
        self.interval = config.get("min_interval_seconds", 2)
        self.emojis = {
            **dict(accepted="👌", running="⏳", done="✅", failed="❌"),
            **config.get("emojis", {}),
        }
        self.pending, self.workers, self.changed = {}, {}, {}
        self.finalizations, self.owners = {}, {}
        self.heavy_results = {}
        self.last_sent = {}
        self.stopping = False

    async def eligible(self, trace, *, finalization=None):
        finalization = finalization or self.finalizations.get(trace)
        r, event = (
            await finalization.persist(self._source, trace)
            if finalization is not None
            else await run_sync(self.ledger, self._source, trace)
        )
        if not self.enabled or r["kind"] != "task" or not event:
            return False
        if self.audience == "owner" and r["requester"]["role"] != "owner":
            return False
        return bool(
            event.get("message")
            and self.delivery.channels[r["channel_id"]].features().supports_react
        )

    def record_heavy_result(self, trace, status):
        self.heavy_results[trace] = status

    def forget(self, trace):
        self.heavy_results.pop(trace, None)

    async def request(self, trace, state, *, finalization=None):
        outcome = self.heavy_results.pop(trace, None) if state in {"done", "failed"} else None
        finalization = finalization or self.finalizations.get(trace)
        if not await self.eligible(trace, finalization=finalization):
            return
        if state == "done" and outcome is not None and outcome != "ok":
            state = (
                await finalization.persist(self._outcome, trace)
                if finalization is not None
                else await run_sync(self.ledger, self._outcome, trace)
            )
        self.pending[trace] = state
        self.changed.setdefault(trace, asyncio.Event()).set()
        if trace not in self.workers:
            # Reserve the first state before a subsequent request can replace it.
            first = self.pending.pop(trace) if trace not in self.last_sent else None
            self.workers[trace] = background(self._run(trace, first))
        if finalization is not None:
            self.owners[trace] = finalization
            finalization.track(self.workers[trace])

    def _outcome(self, trace):
        with self.ledger.transaction():
            self.ledger.audit("task.reaction", trace_id=trace, reaction_outcome="tool_failed")
        return "failed"

    def begin_stop(self):
        self.stopping = True
        for changed in self.changed.values():
            changed.set()

    async def _run(self, trace, state=None):
        try:
            while state is not None or trace in self.pending:
                if state is None:
                    self.changed[trace].clear()
                    remaining = max(
                        0,
                        self.last_sent.get(trace, -self.interval)
                        + self.interval
                        - self.clock.monotonic(),
                    )
                    if self.stopping and self.pending[trace] in {"done", "failed"}:
                        remaining = 0
                    if remaining:
                        timer = asyncio.create_task(self.clock.sleep(remaining))
                        changed = asyncio.create_task(self.changed[trace].wait())
                        try:
                            await asyncio.wait(
                                {timer, changed}, return_when=asyncio.FIRST_COMPLETED
                            )
                        finally:
                            for task in (timer, changed):
                                task.cancel()
                            await asyncio.gather(timer, changed, return_exceptions=True)
                        continue
                    state = self.pending.pop(trace)
                if await self._send(trace, state) == "sent":
                    self.last_sent[trace] = self.clock.monotonic()
                if state in {"done", "failed"}:
                    self.last_sent.pop(trace, None)
                state = None
        finally:
            self.workers.pop(trace, None)
            self.owners.pop(trace, None)
            self.changed.pop(trace, None)

    def _source(self, trace):
        record = self.ledger.get("record", trace)
        return record, self.ledger.source_inbound(record)

    def _register(self, trace, state):
        r, event = self._source(trace)
        previous = [
            i for i in self.ledger.intents(trace) if i["status_reaction"] and i["status"] == "sent"
        ]
        intent = DeliveryIntent(
            intent_id=uuid4().hex,
            companion_id=r["companion_id"],
            conversation_id=r["conversation_id"],
            channel_id=r["channel_id"],
            kind="react",
            emoji=self.emojis[state],
            replaces_emoji=previous[-1]["emoji"] if previous else None,
            target=event["message"]["ref"],
            task_id=r["task_id"],
            trace_id=trace,
        )
        self.ledger.register_intents(trace, [intent], reaction=state)
        return intent

    async def _send(self, trace, state):
        parent = self.owners.get(trace) or self.finalizations.get(trace)
        finalization = parent or OwnedFinalization(self.delivery)

        async def send():
            intent = await finalization.persist(self._register, trace, state)
            return await self.delivery.send_one(intent.intent_id, finalization=finalization)

        follow = finalization.follow if parent is not None else finalization.run
        return await follow(send(), cancel=lambda: None)

    async def restart(self, interrupted):
        for trace in interrupted:
            previous = [
                i
                for i in await run_sync(self.ledger, self.ledger.intents, trace)
                if i["status_reaction"] and i["status"] == "sent"
            ]
            if (
                previous
                and previous[-1]["reaction_state"] in {"accepted", "running"}
                and await self.eligible(trace)
            ):
                # 记录已在重启修正中持久化为 interrupted；下次启动不会再次进入。
                await self._send(trace, "failed")

    async def drain(self):
        if self.workers:
            await asyncio.gather(*tuple(self.workers.values()))
