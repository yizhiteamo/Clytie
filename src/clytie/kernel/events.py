"""每伴侣有序、非持久化事件队列；订阅者失败与递归发布隔离。"""

import asyncio
from collections import defaultdict, deque
from contextvars import Context, ContextVar

from clytie.plugin_host.support import timed

_depth = ContextVar("event_chain_depth", default=0)


def background(coro):
    """不继承调用记录或场景；兼容 Python 3.10。"""
    return Context().run(asyncio.create_task, coro)


class EventBus:
    def __init__(self, clock, log, subscribers, *, capacity=1000, timeout=2):
        self.clock, self.log, self.subscribers = clock, log, subscribers
        self.capacity, self.timeout = capacity, timeout
        self.queues, self.workers = defaultdict(deque), {}
        self.dropped = 0
        self.closed = False

    def publish(self, cid, event):
        if self.closed:
            return False
        depth = _depth.get() + 1
        if depth > 8 or len(self.queues[cid]) >= self.capacity:
            self.dropped += 1
            self.log.warning(
                "event.dropped", reason="depth" if depth > 8 else "capacity", companion_id=cid
            )
            return False
        self.queues[cid].append((event, depth))
        if cid not in self.workers:
            self.workers[cid] = background(self._dispatch(cid))
        return True

    async def _dispatch(self, cid):
        try:
            while self.queues[cid]:
                event, depth = self.queues[cid].popleft()
                token = _depth.set(depth)
                try:
                    for callback in tuple(self.subscribers(cid, event)):
                        try:
                            # Child cancellation is a value; cancelling the dispatcher
                            # cancels gather itself and must propagate.
                            outcomes = await asyncio.gather(
                                timed(callback(event), self.clock, self.timeout),
                                return_exceptions=True,
                            )
                            error = outcomes[0]
                        except Exception as exc:
                            error = exc
                        if isinstance(error, BaseException):
                            self.log.warning(
                                "event.subscriber_failed",
                                companion_id=cid,
                                error=type(error).__name__,
                            )
                finally:
                    _depth.reset(token)
        finally:
            self.workers.pop(cid, None)

    async def drain(self):
        while self.workers:
            await asyncio.gather(*tuple(self.workers.values()))

    async def close(self):
        self.closed = True
        workers = tuple(self.workers.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self.queues.clear()
