"""先投影再检索；派生向量永不作为事实来源。"""

import math

from clytie.sdk import CancelToken

from .store import BASIS, canonical, fingerprint, query_tokens


class HostExtension:
    def __init__(self, host, companion_id, registration):
        self.host, self.cid, self.registration = host, companion_id, registration

    @property
    def cache_identity(self):
        from clytie.config import thaw

        runtime = self.host.runtimes.get((self.cid, self.registration.plugin_id))
        if runtime is None:
            return fingerprint({"plugin": self.registration.plugin_id, "state": "inactive"})
        reg = next(
            (
                r
                for r in runtime.registrations
                if r.point == self.registration.point and r.key == self.registration.key
            ),
            self.registration,
        )
        return fingerprint(
            {
                "plugin": reg.plugin_id,
                "version": runtime.manifest.data["version"],
                "declaration": thaw(reg.declaration),
                "config": thaw(runtime.ctx.config),
            }
        )

    async def embed(self, texts, cancel):
        reg = self.registration
        return await self.host.invoke(
            self.cid, reg.plugin_id, reg.point, reg.key, texts, cancel, method="embed"
        )

    async def rerank(self, query, records, cancel):
        reg = self.registration
        return await self.host.invoke(
            self.cid, reg.plugin_id, reg.point, reg.key, query, records, cancel, method="rerank"
        )


class CandidateReader:
    def __init__(self, store, scene, conversation_id, allowed_visibility=None):
        self.store, self.scene, self.conversation_id = store, scene, conversation_id
        self.allowed = set(allowed_visibility or ["private", "public", "relationship"])

    def records(self):
        return [
            r
            for r in self.store.project(self.scene, self.conversation_id)
            if r.visibility in self.allowed and not r.decay.frozen
        ]

    async def iter(self, *, kinds=None, scope=None, updated_after=None, batch=200):
        if batch < 1:
            raise ValueError("batch must be positive")
        rows = [
            r
            for r in self.records()
            if (kinds is None or r.kind in kinds)
            and (scope is None or r.scope == scope)
            and (updated_after is None or r.updated_at > updated_after)
        ]
        for start in range(0, len(rows), batch):
            yield rows[start : start + batch]

    async def get(self, record_ids):
        ids = set(record_ids)
        return [r for r in self.records() if r.record_id in ids]

    async def count(self):
        return len(self.records())

    async def filter_hits(self, hits):
        ids = {r.record_id for r in self.records()}
        return [hit for hit in hits if hit.record_id in ids]


def vector(values):
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("invalid vector")
    result = [float(v) for v in values]
    if not all(math.isfinite(v) for v in result):
        raise ValueError("invalid vector")
    return result


def cosine(left, right):
    if len(left) != len(right):
        raise ValueError("vector dimension changed")
    length = math.sqrt(sum(v * v for v in left) * sum(v * v for v in right))
    return sum(a * b for a, b in zip(left, right)) / length if length else 0.0


class Retrieval:
    def __init__(self, store, *, embedder=None, embedder_id=None, reranker=None, log=None):
        self.store, self.clock = store, store.clock
        self.embedder, self.embedder_id, self.reranker = embedder, embedder_id, reranker
        self.log = log
        self.last_warning = None
        self.warnings = []
        self.progress = {"complete": 0, "total": 0}

    @property
    def embedder_id(self):
        return getattr(self.embedder, "cache_identity", self._embedder_id)

    @embedder_id.setter
    def embedder_id(self, value):
        self._embedder_id = value

    def warn(self):
        self.warnings = ["向量模型不可用，仅使用关键词"]
        if not self.store.meta("embedder_unavailable", False):
            with self.store.transaction():
                self.store.set_meta("embedder_unavailable", True)
        now = self.clock.monotonic()
        if self.last_warning is None or now - self.last_warning >= 3600:
            self.last_warning = now
            with self.store.transaction():
                self.store.audit("memory_embedder_unavailable")
            if self.log:
                self.log.warning("memory.embedder_unavailable")

    async def rebuild(self, cancel=None):
        cancel = cancel or CancelToken()
        records = [r for r in self.store.all_records() if not r.retracted]
        self.progress = {"complete": 0, "total": len(records)}
        with self.store.transaction():
            self.store.set_meta("embedding_progress", self.progress)
        if self.embedder is None:
            self.warn()
            return
        from clytie.plugin_host.support import timed

        for record in records:
            cancel.raise_if_cancelled()
            old = self.store.db.execute(
                "SELECT 1 FROM vectors WHERE record_id=? AND revision=? AND embedder_id=?",
                (record.record_id, record.revision, self.embedder_id),
            ).fetchone()
            if not old:
                identifier = self.embedder_id
                try:
                    result = await timed(
                        self.embedder.embed([record.content], cancel), self.clock, 30
                    )
                    if len(result) != 1:
                        raise ValueError("invalid embedding result")
                    values = vector(result[0])
                    current = self.store.get_raw(record.record_id)
                    if (
                        current
                        and current.revision == record.revision
                        and identifier == self.embedder_id
                    ):
                        with self.store.transaction():
                            self.store.db.execute(
                                "INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)",
                                (record.record_id, record.revision, identifier, canonical(values)),
                            )
                except Exception:
                    self.warn()
                    continue
            self.progress["complete"] += 1
            with self.store.transaction():
                self.store.set_meta("embedding_progress", self.progress)
        if self.progress["complete"] == self.progress["total"]:
            with self.store.transaction():
                self.store.set_meta("embedder_unavailable", False)

    async def search(
        self, query, scene, conversation_id, *, limit=None, cancel=None, include_frozen=False
    ):
        import json

        from clytie.plugin_host.support import timed

        cancel = cancel or CancelToken()
        cancel.raise_if_cancelled()
        config = self.store.config.get("memory", {})
        records = self.store.project(scene, conversation_id)
        by_id = {r.record_id: r for r in records}
        words = query_tokens(query)
        ordered_words = sorted(words)
        lexical_scores = {}
        for start in range(0, len(ordered_words), 400):
            batch = ordered_words[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            for row in self.store.db.execute(
                f"SELECT record_id,count(*) FROM keywords WHERE token IN ({placeholders}) GROUP BY record_id",
                batch,
            ):
                if row[0] in by_id:
                    lexical_scores[row[0]] = lexical_scores.get(row[0], 0) + row[1]
        routes = [sorted(lexical_scores.items(), key=lambda pair: (-pair[1], pair[0]))]
        semantic_ids = set()
        if self.embedder is None:
            self.warn()
        else:
            try:
                response = await timed(self.embedder.embed([query], cancel), self.clock, 30)
                if len(response) != 1:
                    raise ValueError("invalid embedding result")
                q = vector(response[0])
                scores = []
                for row in self.store.db.execute(
                    "SELECT * FROM vectors WHERE embedder_id=?", (self.embedder_id,)
                ):
                    record = by_id.get(row["record_id"])
                    if record and record.revision == row["revision"]:
                        score = cosine(q, vector(json.loads(row["vector"])))
                        if score > config.get("semantic_min_similarity", 0.5):
                            scores.append((record.record_id, score))
                semantic_ids = {key for key, _ in scores}
                routes.append(sorted(scores, key=lambda p: (-p[1], p[0])))
                self.warnings = []
                if self.store.meta("embedder_unavailable", False):
                    with self.store.transaction():
                        self.store.set_meta("embedder_unavailable", False)
            except Exception:
                self.warn()
        fused = {}
        for route in routes:
            rank, previous = 0, None
            for position, (key, score) in enumerate(route, 1):
                if score != previous:
                    rank = position
                previous = score
                fused[key] = fused.get(key, 0.0) + 1 / (60 + rank)
        factor = {key: 0.5 + 0.5 * by_id[key].decay.weight for key in fused}

        def ordered(keys, scores):
            return sorted(
                keys,
                key=lambda k: (
                    -scores[k],
                    -BASIS[by_id[k].basis],
                    -by_id[k].updated_at.timestamp(),
                    k,
                ),
            )

        raw_order = ordered(fused, fused)
        adjusted = {key: score * factor[key] for key, score in fused.items()}
        ranked = ordered(fused, adjusted)
        if self.reranker and raw_order:
            candidates = raw_order[: config.get("rerank_candidates", 30)]
            try:
                scores = vector(
                    await timed(
                        self.reranker.rerank(query, [by_id[k] for k in candidates], cancel),
                        self.clock,
                        30,
                    )
                )
                if len(scores) != len(candidates):
                    raise ValueError("invalid reranker result")
                raw = dict(zip(candidates, scores))
                ranks, previous, rank = {}, None, 0
                for position, key in enumerate(ordered(candidates, raw), 1):
                    if raw[key] != previous:
                        rank = position
                    previous = raw[key]
                    ranks[key] = factor[key] / (60 + rank)
                ranked = ordered(candidates, ranks) + ordered(
                    raw_order[len(candidates) :], adjusted
                )
            except Exception:
                pass
        cancel.raise_if_cancelled()
        active = [k for k in ranked if not by_id[k].decay.frozen]
        frozen = (
            [
                k
                for k in ranked
                if by_id[k].decay.frozen and k in lexical_scores and k in semantic_ids
            ][:3]
            if include_frozen
            else []
        )
        return [
            by_id[k]
            for k in active[: limit if limit is not None else config.get("recall_limit", 10)]
            + frozen
        ]
