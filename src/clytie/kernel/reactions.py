"""任务状态回应；独立于正文交付汇总，失败不重试、不转文字。"""

import asyncio
from uuid import uuid4

from clytie.backup.coordination import run_sync
from clytie.sdk.types import DeliveryIntent

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

    def eligible(self, trace):
        r = self.ledger.get("record", trace)
        if not self.enabled or r["kind"] != "task" or not r["source_event_id"]:
            return False
        if self.audience == "owner" and r["requester"]["role"] != "owner":
            return False
        event = self.ledger.get("inbound", r["source_event_id"])
        return bool(
            event.get("message")
            and self.delivery.channels[r["channel_id"]].features().supports_react
        )

    def request(self, trace, state):
        if not self.eligible(trace):
            return
        self.pending[trace] = state
        self.changed.setdefault(trace, asyncio.Event()).set()
        if trace not in self.workers:
            self.workers[trace] = background(self._run(trace))

    async def _run(self, trace):
        try:
            while trace in self.pending:
                self.changed[trace].clear()
                state = self.pending[trace]
                if state not in {"done", "failed"} and self.interval:
                    timer = asyncio.create_task(self.clock.sleep(self.interval))
                    changed = asyncio.create_task(self.changed[trace].wait())
                    done, _ = await asyncio.wait(
                        {timer, changed}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in (timer, changed):
                        if task not in done:
                            task.cancel()
                    await asyncio.gather(timer, changed, return_exceptions=True)
                    if changed in done:
                        continue
                state = self.pending.pop(trace)
                await self._send(trace, state)
        finally:
            self.workers.pop(trace, None)

    async def _send(self, trace, state):
        r = self.ledger.get("record", trace)
        event = self.ledger.get("inbound", r["source_event_id"])
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
        await run_sync(self.ledger, self.ledger.register_intents, trace, [intent], reaction=state)
        await self.delivery.send_one(intent.intent_id)

    async def restart(self, interrupted):
        for trace in interrupted:
            previous = [
                i
                for i in self.ledger.intents(trace)
                if i["status_reaction"] and i["status"] == "sent"
            ]
            if (
                previous
                and previous[-1]["reaction_state"] in {"accepted", "running"}
                and self.eligible(trace)
            ):
                # 记录已在重启修正中持久化为 interrupted；下次启动不会再次进入。
                await self._send(trace, "failed")

    async def drain(self):
        if self.workers:
            await asyncio.gather(*tuple(self.workers.values()))
