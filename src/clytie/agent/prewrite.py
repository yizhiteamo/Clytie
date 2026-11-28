"""一次批次的首轮模型结果，仅在内存里等待最终意见。"""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

from clytie.plugin_host.support import timed
from clytie.sdk import CancelToken

from .materials import Materials
from .policy import fast_cancel


@dataclass
class ContextUse:
    memories: dict = field(default_factory=dict)
    partial_groups: set = field(default_factory=set)


@dataclass
class Draft:
    trace: str
    token: CancelToken
    task: asyncio.Task
    watermark: int
    use: ContextUse


class Prewrites:
    def __init__(self, agent):
        self.agent = agent
        self.active = {}

    async def start(self, batch):
        agent = self.agent
        event = batch["events"][-1]
        conversation = event.conversation
        cid = conversation.conversation_id
        if batch.get("draft") or batch.get("tries", 0) >= 3 or agent.shutting_down:
            return
        if cid in agent.opening or fast_cancel(event.message, agent.cancel_words()) is not None:
            return
        finalization = batch["finalizations"][0]
        watermark = await finalization.read(agent.ledger.prepare_watermark, cid)
        if watermark is None or cid in agent.kernel.queue.running:
            return
        if agent.shutting_down or event is not batch["events"][-1] or cid in agent.opening:
            return
        use = ContextUse()
        trace = uuid4().hex
        token = CancelToken()
        events = tuple(batch["events"])
        deadline = agent.clock.now() + timedelta(
            seconds=agent.config.get("agent", {}).get("model_call_seconds", 120)
        )

        async def first():
            async with agent._presence(trace, conversation, event.message.sender, events):
                materials = Materials()
                await agent._wait_for_images(events)
                specs, candidates, images, messages = await agent._round_context(
                    trace,
                    conversation,
                    event.message.sender,
                    events,
                    batch["mode"],
                    "",
                    False,
                    [],
                    0,
                    [],
                    [],
                    use,
                    materials=materials,
                    catch_up=batch.get("catch_up"),
                )
                token.raise_if_cancelled()
                if agent.clock.now() >= deadline:
                    raise TimeoutError
                exhausted = agent.config.get("agent", {}).get("max_tool_rounds", 4) == 0
                turn = await agent._generate(
                    trace,
                    messages,
                    [] if exhausted else specs,
                    conversation,
                    token,
                    finalization,
                    prepared=True,
                    deadline=deadline,
                )
                if turn.finish not in {"stop", "tool_calls"}:
                    raise ValueError("prepare_failed")
                return specs, candidates, images, turn, materials

        async def bounded():
            token.raise_if_cancelled()
            worker = finalization.track(asyncio.create_task(first()))
            try:
                return await timed(
                    worker, agent.clock, max(0, (deadline - agent.clock.now()).total_seconds())
                )
            finally:
                # A cancelled plugin can finish late. Keep its memory session until it exits.
                if not worker.done():
                    worker.add_done_callback(lambda _: agent.memory.sessions.pop(trace, None))

        task = finalization.track(asyncio.create_task(bounded()))
        batch["draft"] = Draft(trace, token, task, watermark, use)
        batch["tries"] = batch.get("tries", 0) + 1
        self.active[trace] = batch

    async def result(self, batch):
        draft = batch["draft"]
        try:
            return await asyncio.shield(draft.task)
        except asyncio.CancelledError:
            if not draft.task.cancelled():
                raise
        except Exception:
            pass
        await self.discard(batch, "stopped" if self.agent.shutting_down else "error")
        return None

    def adopt(self, batch):
        draft = batch.pop("draft")
        self.active.pop(draft.trace)

    async def discard(self, batch, reason):
        draft = batch.pop("draft", None)
        if draft is None:
            return
        self.active.pop(draft.trace)
        draft.token.cancel(reason)
        draft.task.cancel()
        draft.task.add_done_callback(lambda _: self.agent.memory.sessions.pop(draft.trace, None))

        def audit():
            with self.agent.ledger.transaction():
                self.agent.ledger.audit(
                    "prepare_discarded",
                    trace_id=draft.trace,
                    conversation_id=batch["events"][0].conversation.conversation_id,
                    reason=reason,
                )

        await batch["finalizations"][0].persist(audit)

    async def stop(self):
        for batch in list(self.active.values()):
            # The ledger guard decides adoption once the open command has been submitted.
            if not batch.get("opening_record"):
                await self.discard(batch, "stopped")
