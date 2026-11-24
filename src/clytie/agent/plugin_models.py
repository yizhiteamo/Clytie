"""插件的文字模型调用：共用当前模型，按调用插件限制并发与用量。"""

import asyncio
from collections import defaultdict, deque
from datetime import timedelta
from zoneinfo import ZoneInfo

from clytie.kernel.delivery import OwnedFinalization
from clytie.sdk.types import ModelRequest, ModelTurn

DAILY_USAGE_SQL = """SELECT json_extract(data,'$.plugin'), count(*),
                      sum(coalesce(json_extract(data,'$.input_tokens'),0)
                        + coalesce(json_extract(data,'$.output_tokens'),0))
                 FROM audit WHERE json_extract(data,'$.kind')='model.usage'
                   AND json_extract(data,'$.purpose')='plugin' AND json_extract(data,'$.day')=?
                 GROUP BY json_extract(data,'$.plugin')"""


def error_turn(code):
    return ModelTurn(finish="error", usage={}, error={"code": code, "message": ""})


class PluginModels:
    def __init__(self, agent):
        self.agent = agent
        self.starts = defaultdict(deque)
        self.running = set()
        self.zone = ZoneInfo(agent.instance.get("timezone", "UTC"))

    async def generate(self, pid, stopped, req, cancel, *, purpose):
        agent = self.agent
        clock = agent.clock
        if (
            not isinstance(req, ModelRequest)
            or req.tools
            or any(
                m.role == "tool" or m.tool_calls or any(p.type != "text" for p in m.parts)
                for m in req.messages
            )
        ):
            return error_turn("invalid_request")
        if agent.protection.active:
            return error_turn("quota_exhausted")
        now = clock.now()
        deadline = min(req.budget.deadline, now + timedelta(seconds=60))
        if deadline <= now:
            return ModelTurn(finish="deadline", usage={})
        if deadline != req.budget.deadline:
            req = req.model_copy(
                update={"budget": req.budget.model_copy(update={"deadline": deadline})}
            )
        start = clock.monotonic()
        starts = self.starts[pid]
        while starts and starts[0] <= start - 60:
            starts.popleft()
        if pid in self.running or len(starts) >= 10:
            return error_turn("rate_limited")

        finalization = OwnedFinalization(agent.kernel.delivery)
        starts.append(start)
        self.running.add(pid)
        finalization.on_complete(lambda: self.running.discard(pid))
        child = cancel.child()
        model = asyncio.create_task(agent.model_call("generate", req, child, update_health=False))
        day = now.astimezone(self.zone).date().isoformat()

        def audit(turn, duration, timing):
            with agent.ledger.transaction():
                agent.ledger.audit(
                    "model.usage",
                    trace_id=req.trace_id,
                    purpose="plugin",
                    plugin=pid,
                    plugin_purpose=purpose,
                    day=day,
                    duration_ms=duration,
                    **turn.usage.model_dump(),
                    finish=turn.finish,
                    timing=timing,
                )

        async def observe():
            turn = await model
            duration = round((clock.monotonic() - start) * 1000)
            late = child.cancelled or stopped.cancelled or clock.now() >= deadline
            await finalization.persist(audit, turn, duration, "late" if late else "on_time")
            return turn

        observer = finalization.track(asyncio.create_task(observe()))
        waiters = [
            asyncio.create_task(clock.sleep_until(deadline)),
            asyncio.create_task(child.wait()),
            asyncio.create_task(stopped.wait()),
        ]

        async def cancel_after_grace():
            grace = asyncio.create_task(clock.sleep(agent.kernel.grace))
            try:
                await asyncio.wait((model, grace), return_when=asyncio.FIRST_COMPLETED)
                if not model.done():
                    model.cancel()
            finally:
                grace.cancel()
                await asyncio.gather(grace, return_exceptions=True)

        try:
            await asyncio.wait((observer, *waiters), return_when=asyncio.FIRST_COMPLETED)
            stopped.raise_if_cancelled()
            cancel.raise_if_cancelled()
            if clock.now() >= deadline:
                child.cancel("deadline")
                return ModelTurn(finish="deadline", usage={})
            turn = observer.result()
            agent.observe_model_health(turn)
            agent.protection.update(turn.quota)
            return turn
        except asyncio.CancelledError:
            child.cancel(stopped.reason or "cancelled")
            raise
        finally:
            if not model.done():
                child.cancel(stopped.reason or "cancelled")
                finalization.track(asyncio.create_task(cancel_after_grace()))
            for waiter in waiters:
                waiter.cancel()
            finalization.release()
            await asyncio.gather(*waiters, return_exceptions=True)

    def read_status(self):
        rows = self.agent.ledger.db.execute(
            DAILY_USAGE_SQL,
            (self.agent.clock.now().astimezone(self.zone).date().isoformat(),),
        )
        return {pid: dict(count=count, tokens=tokens) for pid, count, tokens in rows}
