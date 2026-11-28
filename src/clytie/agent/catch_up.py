"""保留已认领的入站，按会话放行；补收说明仅供本次模型上下文使用。"""

import asyncio
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from clytie.kernel.delivery import OwnedFinalization


@dataclass
class CatchUpNote:
    historical: set
    source_ids: tuple
    before_count: int
    truncated: str
    retry: bool = False

    @property
    def instructions(self):
        return (
            "这些消息你现在才看到。用你平时说话的方式回应，不要用固定的说法，"
            "不要编造你当时在做什么，不要逐条回复。话题可能已经过去，"
            "可以顺着后来的对话说。"
            + self.truncated
            + ("你之前想回这些消息，但当时没发出去。" if self.retry else "")
        )

    def prior_context(self, events, cutoff, visible):
        shown = {event.event_id for event in events}
        first = datetime.fromisoformat(cutoff.replace("Z", "+00:00")) if cutoff else None
        preceding = []
        selected = {e.event_id for e in events if first is None or e.message.sent_at < first}
        if not self.before_count or not selected:
            return
        recent = deque(maxlen=self.before_count)
        for key in self.source_ids:
            if key in selected:
                preceding.extend(recent)
            if key in visible:
                recent.append(key)
        for key in preceding:
            if key not in shown:
                shown.add(key)
                yield visible[key]


def addressed_selection(events, people_limit, message_limit):
    speakers, selected = set(), set()
    for event in reversed(events):
        flags = event.addressed
        if not (flags.mention or flags.reply_to_self or flags.name):
            continue
        actor = event.message.sender.actor_id
        if len(speakers) >= people_limit and actor not in speakers:
            continue
        speakers.add(actor)
        selected.add(event.event_id)
        if len(selected) == message_limit:
            break
    return selected


@dataclass
class HeldConversation:
    agent: object
    private: bool
    batch_id: object = field(default_factory=object)
    live: deque = field(default_factory=deque)
    history: list = field(default_factory=list)
    waiting: bool = True
    unseen: int = 0
    retry_traces: set = field(default_factory=set)
    judged: int = 0
    judged_respond: int = 0
    skipped: int = 0
    finalization: object = None
    draining: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reserve(self, delivery):
        # 同一次补收顺序写入，所有已接受的消息共用一个串行收尾名额。
        if self.finalization is None or self.finalization.released:
            self.finalization = OwnedFinalization(delivery)
        else:
            self.finalization.leases += 1
        return self.finalization

    def stage(self, event, responsibility, state):
        if not event.message or not (state["historical"] or self.waiting):
            return False
        rows = self.history if state["historical"] else self.live
        rows.append((event, responsibility, state))
        return True

    async def stage_retries(self, retries):
        for event, trace in retries:
            if not await self.agent.admission.accept_checked(event):
                continue
            if self.agent.boundary.filtered(event) or self.agent.boundary.paused(
                event.message.sender
            ):
                continue
            responsibility = self.reserve(self.agent.kernel.delivery)
            self.history.append(
                (
                    event,
                    responsibility,
                    {"holding": self, "historical": True, "retry": trace, "handed": True},
                )
            )

    async def _release(self, row, *, reason=None, note=None, assemble=False):
        event, responsibility, state = row
        state["handed"] = False
        try:
            reason = self.guard_reason(event) or reason
            if reason:
                if reason == "catch_up" and state.get("omit_trace"):
                    await responsibility.persist(
                        self.agent.ledger.skip_catch_up, [], [state["retry"]]
                    )
                elif not state.get("retry"):
                    await responsibility.persist(self.agent.ledger.ignore, event.event_id, reason)
                return reason
            return await self.agent._process_claimed(
                event, responsibility, state, batch_note=note, assemble=assemble
            )
        except Exception as exc:
            if state.get("trace") and not state["handed"]:
                await self.agent.kernel.seal(
                    state["trace"],
                    "error",
                    error={"code": getattr(exc, "code", "agent_error"), "message": ""},
                )
            await responsibility.persist(self.agent._ignore_unhandled, [event.event_id])
            self.agent.log.warning("catch_up.inbound_failed", code="inbound_error")
            return None
        finally:
            if not state["handed"]:
                responsibility.release()

    async def release_live(self):
        async with self.draining:
            while self.live:
                await self._release(self.live.popleft())
            self.waiting = False

    def guard_reason(self, event):
        if self.agent.shutting_down:
            return "shutting_down"
        if self.agent.kernel.delivery.silenced:
            return "silenced"
        if self.agent.model_health == "auth_invalid":
            return "model_auth_invalid"
        if self.agent.boundary.paused(event.message.sender):
            return "boundary_paused"
        return None

    async def should_respond(self, batch):
        events = batch["events"]
        event = events[-1]
        owner = batch["finalizations"][0]
        reason = self.guard_reason(event)
        if not reason and not self.private and event.message.sender.role != "owner":
            if await owner.read(self.agent.safety_belt.paused, event.conversation.conversation_id):
                reason = "safety_paused"
        if reason:
            await owner.persist(self.agent._ignore_unhandled, [e.event_id for e in events], reason)
            return False
        if self.private or "stale_judgment" in batch:
            return True
        age = self.agent.clock.now() - max(e.message.sent_at for e in events)
        if age <= timedelta(minutes=batch["stale_minutes"]):
            return True
        result = await self.agent.judgments.stale(event.conversation, events, age, owner)
        self.judged += 1
        if result.status == "ok" and result.join and result.calling_me is not False:
            batch["stale_judgment"] = result
            self.judged_respond += 1
            return True
        sources = batch.get("retry_sources", {})
        await owner.persist(
            self.agent.ledger.skip_catch_up,
            [e.event_id for e in events if e.event_id not in sources],
            list(dict.fromkeys(sources.values())),
        )
        self.skipped += len(events)
        return False

    async def finish(self, settings, full, source_ids=None):
        history, self.history = self.history, []
        history.sort(key=lambda row: row[0].message.sent_at)
        events = [row[0] for row in history]
        ids = {event.event_id for event in events}
        if self.private:
            selected = ids
        else:
            selected = addressed_selection(
                events, settings["group_people_max"], settings["group_addressed_max"]
            )
        truncated = (
            ("更早还有消息没看到。" if self.private else "这之前还有一段没看到。") if full else ""
        )
        note = (
            CatchUpNote(
                ids,
                source_ids or (),
                0 if self.private else settings["group_context_before"],
                truncated,
            )
            if events
            else None
        )
        response = []
        recorded = responding = 0
        selected_traces = {state.get("retry") for e, _, state in history if e.event_id in selected}
        for row in history:
            if row[0].event_id in selected:
                response.append(row)
            else:
                trace = row[2].get("retry")
                if trace and trace not in selected_traces:
                    row[2]["omit_trace"] = True
                    selected_traces.add(trace)
                recorded += await self._release(row, reason="catch_up") == "catch_up"
        if self.private and source_ids is not None:
            response.extend(self.live)
            self.live.clear()
        # A reaction-only directive for these replies is also subject to the
        # batch's stale decision and its single atomic record.
        retry_actors = {e.message.sender.actor_id for e, _, state in response if state.get("retry")}
        reactions_by_batch = {}
        for row in response:
            event, _, state = row
            state["batch_reaction"] = event.message.sender.actor_id in retry_actors or (
                not self.private
                and self.agent.clock.now() - event.message.sent_at
                > timedelta(minutes=settings["group_stale_minutes"])
            )
            key = await self._release(row, note=note, assemble=True)
            if isinstance(key, tuple):
                reactions_by_batch.setdefault(key, []).append(state.get("reaction"))
                batch = self.agent.batches[key]
                batch["recovery"] = self
                batch["stale_minutes"] = settings["group_stale_minutes"]
                if trace := row[2].get("retry"):
                    batch.setdefault("retry_sources", {})[row[0].event_id] = trace
                    if not batch["catch_up"].retry:
                        batch["catch_up"] = replace(batch["catch_up"], retry=True)
                    self.retry_traces.add(trace)
                responding += row[0].event_id in selected
        for key, reactions in reactions_by_batch.items():
            if key in self.agent.batches:
                if all(reactions):
                    self.agent.batches[key]["reaction"] = reactions[-1]
                try:
                    await self.agent.attention(key)
                except Exception as exc:
                    self.agent.log.warning(
                        "catch_up.inbound_failed",
                        error_type=type(exc).__name__,
                        code="inbound_error",
                    )
        await self.release_live()
        return self.unseen, responding - self.skipped, recorded + self.skipped
