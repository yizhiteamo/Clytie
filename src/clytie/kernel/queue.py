"""同会话串行、伴侣并发上限、主人优先和等待提升。"""

import asyncio

from .events import background


class ConversationQueue:
    def __init__(self, clock, *, maximum=2):
        self.clock, self.maximum = clock, maximum
        self.pending, self.running = [], {}
        self.sequence = 0

    def submit(self, conversation_id, actor, operation):
        future = asyncio.get_running_loop().create_future()
        self.sequence += 1
        item = (self.sequence, self.clock.monotonic(), conversation_id, actor, operation, future)
        self.pending.append(item)
        self._pump()
        return future

    def _pump(self):
        def priority(item):
            owner = item[3].role == "owner" or self.clock.monotonic() - item[1] > 60
            return (not owner, item[0])

        self.pending.sort(key=priority)
        for item in tuple(self.pending):
            if len(self.running) >= self.maximum:
                break
            cid, future = item[2], item[5]
            if future.cancelled():
                self.pending.remove(item)
                continue
            if cid in self.running:
                continue
            self.pending.remove(item)
            self.running[cid] = background(self._run(item))

    async def _run(self, item):
        _, _, cid, _, operation, future = item
        try:
            result = await operation()
        except asyncio.CancelledError:
            future.cancel()
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
        else:
            if not future.done():
                future.set_result(result)
        finally:
            self.running.pop(cid, None)
            self._pump()

    async def drain(self):
        while self.running:
            await asyncio.gather(*tuple(self.running.values()))
