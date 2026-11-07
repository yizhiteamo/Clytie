"""撤销比对只在本体内执行；临时向量不持久化。"""

import json

from clytie.backup.coordination import run_sync
from clytie.plugin_host.support import timed

from .retrieval import cosine, vector
from .store import normalized


class RetractedGuard:
    def __init__(self, store, retrieval, log):
        self.store, self.retrieval, self.log = store, retrieval, log
        self.last_warning = None

    def degraded(self):
        with self.store.transaction():
            self.store.set_meta(
                "recollect_semantic_failures", self.store.meta("recollect_semantic_failures", 0) + 1
            )
        now = self.store.clock.monotonic()
        if self.last_warning is None or now - self.last_warning >= 3600:
            self.last_warning = now
            self.log.warning("memory.recollect_semantic_unavailable")

    async def rejected(self, candidates, retracted, cancel):
        cancel.raise_if_cancelled()
        exact = {normalized(r.content) for r in retracted}
        rejected = {i for i, item in enumerate(candidates) if normalized(item.content) in exact}
        if not retracted or not candidates:
            return rejected
        retrieval = self.retrieval
        identifier = retrieval.embedder_id
        try:
            if retrieval.embedder is None or identifier is None:
                raise ValueError("no configured embedder")
            originals = []
            for record in retracted:
                cached = self.store.db.execute(
                    "SELECT vector FROM vectors WHERE record_id=? AND embedder_id=? AND revision IN (?,?) ORDER BY revision DESC LIMIT 1",
                    (record.record_id, identifier, record.revision, record.revision - 1),
                ).fetchone()
                previous = self.store.db.execute(
                    "SELECT data FROM revisions WHERE record_id=? AND revision=?",
                    (record.record_id, record.revision - 1),
                ).fetchone()
                if cached and previous and json.loads(previous[0])["content"] == record.content:
                    values = vector(json.loads(cached[0]))
                else:
                    response = await timed(
                        retrieval.embedder.embed([record.content], cancel), self.store.clock, 30
                    )
                    if len(response) != 1:
                        raise ValueError("invalid embedding result")
                    values = vector(response[0])
                originals.append(values)
            response = await timed(
                retrieval.embedder.embed([item.content for item in candidates], cancel),
                self.store.clock,
                30,
            )
            if len(response) != len(candidates) or retrieval.embedder_id != identifier:
                raise ValueError("embedding identity or count changed")
            vectors = [vector(item) for item in response]
            threshold = (
                self.store.config.get("memory", {})
                .get("recollect", {})
                .get("retracted_similarity", 0.85)
            )
            semantic = {
                i
                for i, values in enumerate(vectors)
                if any(cosine(values, old) > threshold for old in originals)
            }
        except Exception:
            cancel.raise_if_cancelled()
            await run_sync(self.store, self.degraded)
            return rejected
        cancel.raise_if_cancelled()
        return rejected | semantic
