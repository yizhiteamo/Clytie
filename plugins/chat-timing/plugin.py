"""官方说话时机插件。判断只通过宿主的 attention 协议请求。"""

from collections import OrderedDict
from dataclasses import dataclass, field

from clytie.sdk.behavior import Defer, Judge, JudgeFacts, Respond, Skip
from clytie.sdk.types import Addressed, ContextSection, TextPart

from .timing import Group, Tuning

BANDS = ("low", "medium", "high")


@dataclass
class ReplyParts:
    pending: set[str] = field(default_factory=set)
    counted: bool = False


def remember(mapping, key, value):
    # 失败、撤回的回复没有 sent 事件，不能无限保留未配对的临时标识。
    mapping[key] = value
    if len(mapping) > 4096:
        del mapping[next(iter(mapping))]


class Plugin:
    async def start(self, ctx):
        self.ctx = ctx
        self.t = Tuning(**ctx.config.get("tuning", {}))
        if self.t.fatigue_start >= self.t.fatigue_stop:
            raise ValueError("fatigue_stop must exceed fatigue_start")
        self.groups = {}
        self.arrivals = OrderedDict()
        self.follow_up_judgments = {}
        self.responding = {}
        self.records = {}
        self.intents = {}

    async def stop(self):
        self.groups.clear()
        self.arrivals.clear()
        self.follow_up_judgments.clear()
        self.responding.clear()
        self.records.clear()
        self.intents.clear()

    def local_now(self):
        return self.ctx.clock.now().astimezone(self.ctx.clock.local_tz)

    def group(self, cid, now):
        if cid not in self.groups:
            self.groups[cid] = Group(self.t, now)
        state = self.groups[cid]
        state.refresh(now, self.local_now().date())
        return state

    def band(self, cid):
        config = self.ctx.config
        activity = config.get("conversations", {}).get(cid, config.get("activity", "medium"))
        local = self.local_now().strftime("%H:%M")
        for period in config.get("schedule", ()):
            start, end = period["from"], period["to"]
            active = start <= local < end if start <= end else local >= start or local < end
            if active:
                activity = period["activity"]
                break
        return BANDS.index(activity)

    async def incoming(self, event):
        if not event.dedupable or event.message is None:
            return
        now = self.ctx.clock.monotonic()
        # 过了本体的批次期限，旧入站无需再参与合并。
        while self.arrivals and next(iter(self.arrivals.values()))[0] < now - 120:
            self.arrivals.popitem(last=False)
        mark = event.addressed or Addressed()
        follow_up = "none"
        if event.conversation.kind == "group":
            group = self.group(event.conversation.conversation_id, now)
            group.recent_senders.append((event.message.ref, event.message.sender.actor_id))
            follow_up = group.follow_up(event.message.sender.actor_id)
            group.incoming(
                now,
                event.message.sender.actor_id,
                mark.mention or mark.reply_to_self or mark.name is not None,
            )
        self.arrivals[event.message.ref] = (now, mark, follow_up)

    def response(self, event, natural=False, focus=None):
        sender = event.message.sender
        partner = sender.actor_id
        if focus is not None and focus != event.message.ref:
            recent = self.groups[event.conversation.conversation_id].recent_senders
            actor = next((actor for ref, actor in reversed(recent) if ref == focus), None)
            if actor is not None and actor != self.ctx.companion_id:
                partner = actor
        remember(self.responding, event.event_id, (sender.role == "owner", natural, partner))
        return Respond(focus=focus or event.message.ref, recheck_after=15 if natural else None)

    async def attention(self, input):
        event, batch, *results = input
        now = self.ctx.clock.monotonic()
        cid = event.conversation.conversation_id
        private = event.conversation.kind == "private"
        state = None if private else self.group(cid, now)
        arrivals = [self.arrivals[m.ref] for m in batch if m.ref in self.arrivals]
        marks = [mark for _, mark, _ in arrivals] or [event.addressed or Addressed()]
        direct = any(mark.mention or mark.reply_to_self for mark in marks)
        named = any(mark.name for mark in marks)
        if arrivals and not results:
            if private or direct or named:
                due = min(
                    arrivals[-1][0] + self.t.addressed_quiet, arrivals[0][0] + self.t.addressed_max
                )
            else:
                due = min(state.last_in + self.t.group_quiet, arrivals[0][0] + self.t.group_max)
            if now < due:
                return Defer(seconds=due - now, prepare=private or direct or named)
        if private:
            return Respond(focus=event.message.ref if direct or named else None)
        if direct:
            return self.response(event)
        owner = event.message.sender.role == "owner"
        if results:
            result = results[0]
            was_follow_up = self.follow_up_judgments.pop(event.event_id, False)
            if result.status == "ok" and result.new_topic:
                state.fatigue /= 2
            if named:
                return (
                    Skip()
                    if result.status == "ok" and result.calling_me is False
                    else self.response(event)
                )
            if result.status == "failed" and was_follow_up and not owner:
                return Skip()
            if result.status == "failed" or result.join:
                return self.response(event, natural=True, focus=result.focus or event.message.ref)
            return Skip()
        band = self.band(cid)
        follow_up = arrivals[-1][2] if arrivals and not named else "none"
        if follow_up != "none":
            if now - state.last_follow_up_judge < self.t.follow_up_gap_seconds:
                return Skip()
            if not owner and self.ctx.random.random() >= state.fatigue_factor(band):
                return Skip()
            state.last_follow_up_judge = state.last_judge = now
        elif not named:
            if now - state.last_judge < self.t.judge_interval:
                return Skip()
            question = any(
                isinstance(p, TextPart) and ("？" in p.text or "?" in p.text)
                for message in batch
                for p in message.parts
            )
            if self.ctx.random.random() >= state.willingness(band, owner, question, now):
                return Skip()
        if self.ctx.config.get("judge", "model") == "rules":
            if follow_up != "none" and not owner:
                return Skip()
            return self.response(event, natural=not named)
        if follow_up != "none":
            remember(self.follow_up_judgments, event.event_id, True)
        state.last_judge = now
        state.judgments += 1
        people, share = state.share()
        start, stop = state.thresholds(band)
        fatigue = "high" if state.fatigue >= stop else "some" if state.fatigue >= start else "none"
        return Judge(
            prepare=named,
            facts=JudgeFacts(
                participants=people,
                share=share,
                rounds_with_same_person=state.rounds,
                counterpart_fast=state.fast,
                fatigue=fatigue,
                ignored_recently=state.level > 0,
                owner_spoke=owner,
                name_hit=named,
                follow_up=follow_up,
            ),
        )

    async def before_send(self, intents):
        for intent in intents:
            if (
                intent.conversation_id in self.groups
                and intent.kind == "reply"
                and intent.task_id is None
            ):
                if intent.trace_id not in self.intents:
                    remember(self.intents, intent.trace_id, ReplyParts())
                self.intents[intent.trace_id].pending.add(intent.intent_id)

    async def on_event(self, event):
        payload = event.payload
        cid = payload["conversation_id"]
        trace = payload["trace_id"]
        if event.name == "message.received":
            decision = self.responding.pop(payload["event_id"], None)
            if decision is not None:
                remember(self.records, trace, (payload["actor_id"], *decision))
        elif event.name == "reply.sent" and trace in self.records:
            parts = self.intents.get(trace)
            if parts is None or parts.pending.isdisjoint(payload["intent_ids"]):
                return
            parts.pending.difference_update(payload["intent_ids"])
            actor, owner, natural, partner = self.records[trace]
            now = self.ctx.clock.monotonic()
            lag = max(0, (self.ctx.clock.now() - event.at).total_seconds())
            group = self.group(cid, now)
            group.sent_part(partner, now - lag)
            if not parts.counted:
                group.replied(now, actor, owner, natural, sent_at=now - lag)
                parts.counted = True
            if not parts.pending:
                self.records.pop(trace)
                self.intents.pop(trace)

    async def hint(self, request):
        if request.conversation is None or request.conversation.kind != "group":
            return None
        cid = request.conversation.conversation_id
        if (
            cid not in self.groups
            or self.group(cid, self.ctx.clock.monotonic()).fatigue < self.t.fatigue_start
        ):
            return None
        return ContextSection(
            title="说话时机",
            text="在这个群聊了挺久，有点累了。可以说得简短些，想收尾就自然收个尾。",
            priority=100,
        )

    async def status(self, request):
        groups = {}
        for cid in self.groups:
            state = self.group(cid, self.ctx.clock.monotonic())
            anonymous = cid.startswith("anon:")
            label = "未登记的群" if anonymous else cid
            row = groups.setdefault(
                label,
                dict(
                    activity=BANDS[self.band(None if anonymous else cid)],
                    fatigue=0.0,
                    ignored_level=0,
                    judgments_today=0,
                    natural_today=0,
                ),
            )
            row["fatigue"] = max(row["fatigue"], state.fatigue)
            row["ignored_level"] = max(row["ignored_level"], state.level)
            row["judgments_today"] += state.judgments
            row["natural_today"] += state.natural
        return {"status": 200, "json": {"groups": groups}}
