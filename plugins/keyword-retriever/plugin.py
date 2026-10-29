"""只对本体提供的可见候选记录进行关键词检索。"""

from clytie.sdk.types import MemoryHit


class KeywordRetriever:
    def __init__(self, ctx):
        self.ctx = ctx

    async def search(self, q, candidates, cancel):
        cancel.raise_if_cancelled()
        await self.ctx.clock.sleep(0)
        words = set(q.text.casefold().split())
        hits = []
        async for batch in candidates.iter(scope=q.scope):
            cancel.raise_if_cancelled()
            for record in batch:
                cancel.raise_if_cancelled()
                matched = sum(word in record.content.casefold() for word in words)
                if matched:
                    hits.append(
                        MemoryHit(
                            record_id=record.record_id,
                            score=matched / len(words),
                            source=self.ctx.plugin_id,
                        )
                    )
        cancel.raise_if_cancelled()
        return sorted(hits, key=lambda hit: (-hit.score, hit.record_id))[: q.limit]


class Plugin:
    async def start(self, ctx):
        self.ctx = ctx
        await ctx.clock.sleep(0)

    def create_retriever(self):
        return KeywordRetriever(self.ctx)

    async def stop(self):
        await self.ctx.clock.sleep(0)
        self.ctx = None
