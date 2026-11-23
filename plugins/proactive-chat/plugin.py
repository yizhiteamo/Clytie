"""主动私聊插件。只提交内部话题，实际交付由本体负责。"""

import asyncio
from pathlib import Path

from .schedule import Calendar, Schedule

DATA = Path(__file__).with_name("data")


def remember(mapping, key, value):
    # 群中的发言不一定得到回复。短期关联只存逻辑引用，不保留消息正文。
    mapping[key] = value
    if len(mapping) > 4096:
        del mapping[next(iter(mapping))]


class Plugin:
    async def start(self, ctx):
        self.ctx = ctx
        self.lock = asyncio.Lock()
        self.conversation = ctx.config.get("conversation")
        self.error = None
        self.schedule = None
        self.calendar = Calendar(DATA)
        self.owner_events = {}
        self.owner_refs = {}
        self.reply_sources = {}
        self.reply_focus = {}
        self.reply_intents = {}
        try:
            if not self.conversation:
                raise ValueError("missing conversation")
            self.schedule = Schedule(ctx.config, ctx.random, ctx.clock.local_tz, DATA)
        except ValueError:
            self.error = "invalid_configuration"
            return
        self.calendar = self.schedule.calendar
        saved = await ctx.storage.get("schedule")
        self.schedule.start(ctx.clock.now(), saved)
        await self.reconcile(ctx.clock.now())
        await self.save()

    async def stop(self):
        async with self.lock:
            if self.schedule is not None:
                await self.save()

    async def save(self):
        await self.ctx.storage.set("schedule", self.schedule.dump())

    async def reconcile(self, now):
        changed = False
        for trace in tuple(self.schedule.state.pending):
            result = await self.ctx.proactive.result(trace)
            if self.schedule.resolve(trace, result, now):
                changed = True
        return changed

    async def incoming(self, event):
        if not event.dedupable or event.message is None or self.schedule is None:
            return
        message = event.message
        owner = message.sender.role == "owner"
        remember(self.owner_refs, message.ref, owner)
        if not owner:
            return
        remember(self.owner_events, event.event_id, True)
        mark = event.addressed
        if event.conversation.kind == "private" or (
            mark is not None and (mark.mention or mark.reply_to_self or mark.name is not None)
        ):
            async with self.lock:
                self.schedule.chat(event.received_at)
                await self.save()

    async def focus(self, draft):
        # 群里的回复可能接同一批中的另一条消息。只取本体确定的回复目标。
        if draft.focus is not None and draft.conversation.kind == "group":
            remember(self.reply_focus, draft.trace_id, self.owner_refs.get(draft.focus, False))

    async def before_send(self, intents):
        for intent in intents:
            if intent.kind == "reply":
                if intent.trace_id not in self.reply_intents:
                    remember(self.reply_intents, intent.trace_id, set())
                self.reply_intents[intent.trace_id].add(intent.intent_id)

    async def on_event(self, event):
        if event.source != "core" or self.schedule is None:
            return
        payload = event.payload
        trace = payload["trace_id"]
        if event.name == "message.received":
            if self.owner_events.pop(payload["event_id"], False):
                remember(self.reply_sources, trace, True)
            return
        if event.name != "reply.sent":
            return
        async with self.lock:
            intents = self.reply_intents.get(trace)
            if not intents or not intents.intersection(payload["intent_ids"]):
                return
            intents.difference_update(payload["intent_ids"])
            if self.reply_focus.get(trace, self.reply_sources.get(trace, False)):
                self.schedule.chat(event.at)
                await self.save()
            if not intents:
                self.reply_sources.pop(trace, None)
                self.reply_focus.pop(trace, None)
                self.reply_intents.pop(trace)

    def instruction(self, opening):
        topic = {
            "good_morning": "你刚起床，可以和主人说早安。",
            "long_silence": "你和主人有一阵子没说话了。想聊就聊，可以分享点什么，也可以画张图。",
            "festival": "",
        }[opening.reason]
        if opening.festivals:
            topic += "今天是" + "、".join(opening.festivals) + "。"
        if self.schedule.state.unanswered:
            topic += "主人之前没回，可能只是没看到。不要追问，也不要抱怨。"
        return topic

    async def on_tick(self, tick):
        async with self.lock:
            if self.error:
                return
            now = self.ctx.clock.now()
            s = self.schedule.state
            new_day = self.schedule.refresh(now)
            stale_submission = s.submitting is not None
            self.schedule.abandon_submission(now)
            changed = await self.reconcile(now)
            opening = self.schedule.next_topic(now)
            if opening is None:
                if new_day or changed or stale_submission:
                    await self.save()
                return
            # 在提交前记下这次机会。即使进程在提交与得到 trace 之间退出，也不会重放。
            self.schedule.reserve(opening, now)
            await self.save()
            try:
                submission = await self.ctx.proactive.submit(
                    self.conversation,
                    topic=self.instruction(opening),
                    reason=opening.reason,
                    tools=True,
                )
            except ValueError:
                self.error = "invalid_conversation"
            else:
                if submission.status == "accepted":
                    self.schedule.attempt(opening, submission.trace_id, now)
            s.submitting = None
            await self.save()

    async def status(self, request):
        async with self.lock:
            now = self.ctx.clock.now()
            plan = self.schedule
            if plan is not None and plan.refresh(now):
                await self.save()
            state = plan.state if plan else None
            return {
                "status": 200,
                "json": {
                    "openings_today": state.openings if state else 0,
                    "attempts_today": state.attempts if state else 0,
                    "declines_today": state.declines if state else 0,
                    "unanswered": state.unanswered if state else 0,
                    "next_opening_at": plan.next_time(now) if not self.error else None,
                    "calendar_complete": self.calendar.day(
                        now.astimezone(self.ctx.clock.local_tz).date()
                    )[1],
                    "configuration_error": self.error,
                },
            }
