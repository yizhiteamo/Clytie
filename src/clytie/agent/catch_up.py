"""保留已认领的入站，按会话放行；补收说明仅供本次模型上下文使用。"""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from clytie.kernel.delivery import OwnedFinalization


@dataclass
class CatchUpNote:
    historical: set
    source_ids: tuple
    before_count: int
    truncated: str

    @property
    def instructions(self):
        return (
            "这些消息你现在才看到。用你平时说话的方式回应，不要用固定的说法，"
            "不要编造你当时在做什么，不要逐条回复。话题可能已经过去，"
            "可以顺着后来的对话说。" + self.truncated
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

    async def _release(self, row, *, reason=None, note=None, assemble=False):
        event, responsibility, state = row
        state["handed"] = False
        try:
            if self.agent.shutting_down:
                reason = "shutting_down"
            elif self.agent.kernel.delivery.silenced:
                reason = "silenced"
            elif self.agent.model_health == "auth_invalid":
                reason = "model_auth_invalid"
            if reason:
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

    async def finish(self, settings, full, source_ids=None):
        history, self.history = self.history, []
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
        for row in history:
            if row[0].event_id in selected:
                response.append(row)
            else:
                recorded += await self._release(row, reason="catch_up") == "catch_up"
        if self.private and source_ids is not None:
            response.extend(self.live)
            self.live.clear()
        keys = {}
        for row in response:
            key = await self._release(row, note=note, assemble=True)
            if isinstance(key, tuple):
                keys[key] = None
                responding += row[0].event_id in selected
        for key in keys:
            if key in self.agent.batches:
                try:
                    await self.agent.attention(key)
                except Exception as exc:
                    self.agent.log.warning(
                        "catch_up.inbound_failed",
                        error_type=type(exc).__name__,
                        code="inbound_error",
                    )
        await self.release_live()
        return self.unseen, responding, recorded
