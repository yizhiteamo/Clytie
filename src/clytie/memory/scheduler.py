"""基于注入时钟的回想调度；不回溯启动基线之前的账本。"""

import asyncio
import json
from datetime import datetime

from clytie.sdk import CancelToken
from clytie.sdk.types import ChatMessage, Conversation, TextPart

from .presence import bind_presence
from .recollection import RecollectionCommit, SnapshotReader
from .retracted import RetractedGuard
from .store import canonical
from .window import build_window, fresh_rows, highwater, involved, latest_activity


class RecollectionModel:
    def __init__(self, agent):
        self.agent = agent

    async def generate(self, request, cancel, *, purpose):
        agent = self.agent
        config = agent.config.get("memory", {}).get("recollect", {})
        selected = config.get("model")
        request = request.model_copy(
            update={
                "messages": [
                    ChatMessage(
                        role="system",
                        parts=[
                            TextPart(
                                text=agent.persona.render(getattr(self, "scene", "public_group"))
                            )
                        ],
                    ),
                    *request.messages,
                ]
            }
        )
        if selected is None:
            result = await agent.model_call("generate", request, cancel)
        else:
            reg = next(
                (
                    r
                    for r in agent.host.registrations(agent.cid, "model")
                    if r.plugin_id == selected["plugin"] and r.key == "recollect_model"
                ),
                None,
            )
            if reg is None:
                raise ValueError("recollection model unavailable")
            result = await agent.host.invoke(
                agent.cid, reg.plugin_id, "model", reg.key, request, cancel, method="generate"
            )
        with agent.ledger.transaction():
            agent.ledger.audit(
                "memory.recollect_model",
                purpose=purpose,
                usage=result.usage.model_dump(mode="json"),
                finish=result.finish,
            )
        agent.protection.update(result.quota)
        return result


class Recollector:
    def __init__(self, agent):
        self.agent, self.store, self.clock = agent, agent.memory.store, agent.clock
        self.config = agent.config.get("memory", {}).get("recollect", {})
        self.guard = RetractedGuard(self.store, agent.memory.retrieval, agent.log)
        self.token = CancelToken()
        self.current = {}
        self.model = RecollectionModel(agent)
        self.conversations = {}
        self.pid = self.config.get("consolidator", "default-consolidator")
        self.baseline()

    def baseline(self):
        row = self.store.db.execute(
            "SELECT data FROM recollect_progress WHERE conversation_id='*'"
        ).fetchone()
        if row is None:
            baseline = highwater(self.agent.ledger)
            with self.store.transaction():
                self.store.db.execute(
                    "INSERT INTO recollect_progress VALUES(?,?)", ("*", canonical(baseline))
                )
        else:
            baseline = json.loads(row[0])
        return baseline

    def progress(self, cid):
        row = self.store.db.execute(
            "SELECT data FROM recollect_progress WHERE conversation_id=?", (cid,)
        ).fetchone()
        return json.loads(row[0]) if row else self.baseline()

    def enabled(self):
        return self.config.get("enabled", True)

    def sync_enabled(self):
        row = self.store.db.execute(
            "SELECT data FROM recollect_progress WHERE conversation_id='!state'"
        ).fetchone()
        state = json.loads(row[0]) if row else {"enabled": self.enabled(), "skipped": 0}
        if not self.enabled():
            for token in tuple(self.current.values()):
                token.cancel("recollection_disabled")
        if self.enabled() and not state["enabled"]:
            end = highwater(self.agent.ledger)
            skipped = 0
            for cid, entry in self.agent.instance.get("conversations", {}).items():
                if entry["scene"] == "stranger_private" or (
                    entry["kind"] == "private" and entry["scene"] != "owner_private"
                ):
                    continue
                rows, _ = fresh_rows(self.agent.ledger, self.progress(cid), cid, skipping=True)
                sizes = [
                    sum(
                        len(p.get("text", ""))
                        for p in (r[3].get("message") or r[3]).get("parts", [])
                    )
                    for r in rows
                ]
                size = 0
                for i, chars in enumerate(sizes):
                    if i == 0 or size + chars > self.config.get("window_chars", 12_000):
                        skipped += 1
                        size = 0
                    size += chars
            with self.store.transaction():
                # 逐个覆盖进度，不删除账本或记忆；未知/已移除会话也不能补回旧窗口。
                ids = {
                    r[0]
                    for r in self.store.db.execute("SELECT conversation_id FROM recollect_progress")
                    if not r[0].startswith("!")
                }
                ids.update(self.agent.instance.get("conversations", {}))
                ids.add("*")
                for cid in ids:
                    self.store.db.execute(
                        "INSERT OR REPLACE INTO recollect_progress VALUES(?,?)",
                        (cid, canonical(end)),
                    )
                state = {"enabled": True, "skipped": skipped}
                self.store.db.execute(
                    "INSERT OR REPLACE INTO recollect_progress VALUES('!state',?)",
                    (canonical(state),),
                )
        elif not row or state["enabled"] != self.enabled():
            state["enabled"] = self.enabled()
            with self.store.transaction():
                self.store.db.execute(
                    "INSERT OR REPLACE INTO recollect_progress VALUES('!state',?)",
                    (canonical(state),),
                )
        return self.enabled()

    async def set_enabled(self, enabled):
        self.config["enabled"] = enabled
        self.sync_enabled()
        if enabled:
            await self.prepare()

    async def start(self):
        if self.sync_enabled():
            await self.prepare()
        task = self.agent.track(self.run())
        jobs = self.agent.host.memory_jobs.setdefault(self.agent.cid, set())
        jobs.add(task)
        task.add_done_callback(jobs.discard)

    async def prepare(self):
        self.agent.host.recollection_models[self.agent.cid, self.pid] = self.model
        selected = self.config.get("model")
        if selected and not self.agent.host.is_active(self.agent.cid, selected["plugin"]):
            plan = self.agent.host.plan_enable(self.agent.cid, selected["plugin"])
            if plan.ok:
                await self.agent.host.apply(plan.plan_id)
        if (
            (
                (
                    self.pid == "default-consolidator"
                    and self.agent.config.get("plugins", {}).get(self.pid, {}).get("enabled")
                    is not False
                )
                or self.agent.config.get("plugins", {}).get(self.pid, {}).get("enabled", False)
            )
            and self.pid in self.agent.host.manifests
            and not self.agent.host.is_active(self.agent.cid, self.pid)
        ):
            plan = self.agent.host.plan_enable(self.agent.cid, self.pid)
            if plan.ok:
                await self.agent.host.apply(plan.plan_id)

    async def run(self):
        try:
            while True:
                await self.clock.sleep(60)
                if not self.sync_enabled():
                    continue
                await self.prepare()
                for cid in self.agent.instance.get("conversations", {}):
                    entry = self.agent.instance["conversations"][cid]
                    self.conversations.setdefault(
                        cid,
                        Conversation(
                            conversation_id=cid,
                            kind=entry["kind"],
                            scene=entry["scene"],
                            channel_id=entry.get("channel", "unknown"),
                        ),
                    )
                for conversation in tuple(self.conversations.values()):
                    try:
                        await self.process(conversation)
                    except Exception:
                        self.agent.log.warning("memory.recollect_failed")
        finally:
            self.token.cancel("closed")

    def activity(self, conversation):
        self.conversations[conversation.conversation_id] = conversation
        token = self.current.get(conversation.conversation_id)
        if token:
            token.cancel("new_activity")

    async def process(self, conversation):
        if not self.sync_enabled():
            return
        cid = conversation.conversation_id
        if cid in self.current:
            return
        token = CancelToken()
        self.current[cid] = token
        try:
            return await self._process(conversation, token)
        except asyncio.CancelledError:
            if not token.cancelled:
                raise
        finally:
            self.current.pop(cid, None)

    async def _process(self, conversation, token):
        if conversation.scene == "stranger_private" or (
            conversation.kind == "private" and conversation.scene != "owner_private"
        ):
            return
        cid = conversation.conversation_id
        progress = self.progress(cid)
        rows, end = fresh_rows(self.agent.ledger, progress, cid)
        if not rows:
            return
        idle = self.config.get("idle_minutes", 30) * 60
        activity = latest_activity(self.agent.ledger, progress, cid)
        latest = max(
            activity, datetime.fromisoformat(progress.get("attempt_at", activity.isoformat()))
        )
        if (self.clock.now() - latest).total_seconds() < idle:
            return
        day = self.clock.now().astimezone(self.clock.local_tz).date().isoformat()
        count = progress.get("count", 0) if progress.get("day") == day else 0
        if count >= self.config.get("daily_limit", 12):
            return
        selected, size = [], 0
        for row in rows:
            parts = (row[3].get("message") or row[3]).get("parts") or []
            chars = sum(len(part.get("text", "")) for part in parts)
            if selected and size + chars > self.config.get("window_chars", 12_000):
                break
            selected.append(row)
            size += chars
        if len(selected) < len(rows):
            end = dict(progress)
            for _, table, sequence, _ in selected:
                end[table] = max(end.get(table, 0), sequence)
        token.raise_if_cancelled()
        actors_present = {
            r[3]["message"]["sender"]["actor_id"] for r in selected if r[1] == "inbound"
        }
        with bind_presence(cid, actors_present):
            reader = SnapshotReader(self.store.project(conversation.scene, cid))
            window, actors, retracted, trace = build_window(
                selected, conversation, reader, self.store, ledger=self.agent.ledger
            )
            items, rejected = [], set()
            relevant = conversation.kind == "private" or involved(
                window, actors, self.agent.instance
            )
            if relevant:
                attempt = dict(
                    progress, day=day, count=count + 1, attempt_at=self.clock.now().isoformat()
                )
                with self.store.transaction():
                    self.store.db.execute(
                        "INSERT OR REPLACE INTO recollect_progress VALUES(?,?)",
                        (cid, canonical(attempt)),
                    )
                try:
                    reg = next(
                        (
                            r
                            for r in self.agent.host.registrations(
                                self.agent.cid, "memory.consolidator"
                            )
                            if r.plugin_id == self.pid
                        ),
                        None,
                    )
                    if reg is None:
                        raise ValueError("consolidator unavailable")
                    self.model.scene = conversation.scene
                    items = await self.agent.host.invoke(
                        self.agent.cid,
                        reg.plugin_id,
                        reg.point,
                        reg.key,
                        window,
                        reader,
                        token,
                        method="run",
                        scene=conversation.scene,
                        conversation_id=cid,
                    )
                    rejected = await self.guard.rejected(items, retracted, token)
                except Exception:
                    attempt["failures"] = attempt.get("failures", 0) + 1
                    with self.store.transaction():
                        self.store.db.execute(
                            "INSERT OR REPLACE INTO recollect_progress VALUES(?,?)",
                            (cid, canonical(attempt)),
                        )
                    raise
                end.update(
                    day=day,
                    count=count + 1,
                    attempt_at=attempt["attempt_at"],
                    failures=attempt.get("failures", 0),
                )
            self.sync_enabled()
            token.raise_if_cancelled()
            return RecollectionCommit(self.store).commit(
                items,
                window,
                reader,
                cid=cid,
                actors=actors,
                trace=trace,
                progress=end,
                cancel=token,
                rejected=rejected,
            )
