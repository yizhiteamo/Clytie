"""基于注入时钟的回想调度；不回溯启动基线之前的账本。"""

import asyncio
import json
from contextvars import ContextVar
from datetime import datetime
from types import SimpleNamespace

from clytie.backup.coordination import run_sync
from clytie.config import default_consolidator
from clytie.kernel.delivery import OwnedFinalization
from clytie.sdk import Cancelled, CancelToken
from clytie.sdk.types import ChatMessage, Conversation, TextPart
from clytie.storage.owner import (
    StorageBusy,
    StorageCancelled,
    StorageFailed,
    cancellable_storage_commit,
    storage_cancel_scope,
)

from .presence import bind_presence
from .recollection import RecollectionCommit, SnapshotReader
from .retracted import RetractedGuard
from .store import canonical
from .window import WindowActors, build_window, fresh_rows, highwater, involved, latest_activity

_operation = ContextVar("recollection_operation", default=None)


class RecollectionModel:
    def __init__(self, agent):
        self.agent = agent

    async def generate(self, request, cancel, *, purpose):
        agent = self.agent
        if getattr(agent, "model_health", "unknown") == "auth_invalid":
            from clytie.sdk.types import ModelTurn

            return ModelTurn(
                finish="error", usage={}, error={"code": "model_unavailable", "message": ""}
            )
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

        def audit():
            with agent.ledger.transaction():
                agent.ledger.audit(
                    "memory.recollect_model",
                    purpose=purpose,
                    usage=result.usage.model_dump(
                        mode="json", include={"input_tokens", "output_tokens"}
                    ),
                    finish=result.finish,
                )

        operation = _operation.get()
        if operation is not None and operation[1]:
            await operation[0].persist(audit)
        else:
            await run_sync(agent.ledger, audit)
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
        self.pid = self.config.get("consolidator", default_consolidator())
        self._initialized = False
        self.delivery = getattr(getattr(agent, "kernel", None), "delivery", None)
        if self.delivery is None:
            self.delivery = SimpleNamespace(ledger=self.store, background=set())

    async def initialize(self, persist=None):
        if not self._initialized:
            await (persist or self._persist)(self.baseline)
            self._initialized = True

    async def _persist(self, function, *args, **kwargs):
        return await run_sync(self.store, function, *args, **kwargs)

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

    async def _sync_enabled(self, persist=None):
        if not self.enabled():
            for token in tuple(self.current.values()):
                token.cancel("recollection_disabled")
        return await (persist or self._persist)(self.sync_enabled)

    async def set_enabled(self, enabled):
        await self.initialize()
        self.config["enabled"] = enabled
        await self._sync_enabled()
        if enabled:
            await self.prepare()

    async def start(self):
        await self.initialize()
        if await self._sync_enabled():
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
        settings = self.agent.config.get("plugins", {}).get(self.pid, {})
        if (
            settings.get("enabled", self.pid == default_consolidator())
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
                try:
                    if not await self._sync_enabled():
                        continue
                    await self.prepare()
                except Exception as exc:
                    self.agent.log.warning("memory.recollect_failed", exc_type=type(exc).__name__)
                    continue
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
                    except Exception as exc:
                        self.agent.log.warning(
                            "memory.recollect_failed", exc_type=type(exc).__name__
                        )
        finally:
            self.token.cancel("closed")

    def activity(self, conversation):
        self.conversations[conversation.conversation_id] = conversation
        token = self.current.get(conversation.conversation_id)
        if token:
            token.cancel("new_activity")

    async def process(self, conversation):
        cid = conversation.conversation_id
        if cid in self.current:
            return
        operation = OwnedFinalization(self.delivery)
        token = CancelToken()
        self.current[cid] = token

        async def process():
            lease = [operation, True]
            binding = _operation.set(lease)
            try:
                await self.initialize(operation.persist)
                if not await self._sync_enabled(operation.persist):
                    return
                return await self._process(conversation, token, operation.persist)
            except asyncio.CancelledError:
                if not token.cancelled:
                    raise
            finally:
                lease[1] = False
                _operation.reset(binding)
                self.current.pop(cid, None)

        return await operation.run(process(), cancel=lambda: token.cancel("closed"))

    def _window_snapshot(self, conversation):
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
        actors_present = {
            r[3]["message"]["sender"]["actor_id"] for r in selected if r[1] == "inbound"
        }
        with bind_presence(cid, actors_present):
            reader = SnapshotReader(self.store.project(conversation.scene, cid))
            window, actors, retracted, trace = build_window(
                selected, conversation, reader, self.store, ledger=self.agent.ledger
            )
            relevant = conversation.kind == "private" or involved(
                window, actors, self.agent.instance
            )
            attempt = None
            if relevant:
                attempt = dict(
                    progress, day=day, count=count + 1, attempt_at=self.clock.now().isoformat()
                )
                self._save_progress(cid, attempt)
            return dict(
                cid=cid,
                day=day,
                count=count,
                end=end,
                attempt=attempt,
                present=actors_present,
                reader=list(reader.rows),
                window=window,
                actors=dict(actors),
                reply_roles=actors.reply_roles,
                retracted=retracted,
                trace=trace,
                relevant=relevant,
            )

    def _save_progress(self, cid, progress):
        with self.store.transaction():
            self.store.db.execute(
                "INSERT OR REPLACE INTO recollect_progress VALUES(?,?)", (cid, canonical(progress))
            )

    async def _process(self, conversation, token, persist):
        token.raise_if_cancelled()
        snapshot = await persist(
            lambda conversation, **_: self._window_snapshot(conversation),
            conversation,
            scene=conversation.scene,
            conversation_id=conversation.conversation_id,
        )
        if snapshot is None:
            return
        cid, end, attempt = snapshot["cid"], snapshot["end"], snapshot["attempt"]
        reader = SnapshotReader(snapshot["reader"])
        window = snapshot["window"]
        actors = WindowActors()
        actors.update(snapshot["actors"])
        actors.reply_roles = snapshot["reply_roles"]
        token.raise_if_cancelled()
        with bind_presence(cid, snapshot["present"]):
            items, rejected = [], set()
            if snapshot["relevant"]:
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
                    rejected = await self.guard.rejected(
                        items, snapshot["retracted"], token, persist=persist
                    )
                except (StorageBusy, StorageCancelled, StorageFailed):
                    raise
                except Exception:
                    attempt["failures"] = attempt.get("failures", 0) + 1
                    await persist(self._save_progress, cid, attempt)
                    raise
                end.update(
                    day=snapshot["day"],
                    count=snapshot["count"] + 1,
                    attempt_at=attempt["attempt_at"],
                    failures=attempt.get("failures", 0),
                )
            await self._sync_enabled(persist)
            token.raise_if_cancelled()
            rolled_back_error = []

            def commit(items, window, records, actor_values, reply_roles, progress, rejected, **_):
                fixed_actors = WindowActors()
                fixed_actors.update(actor_values)
                fixed_actors.reply_roles = reply_roles
                try:
                    with cancellable_storage_commit(token) as decision:
                        return RecollectionCommit(self.store).commit(
                            items,
                            window,
                            SnapshotReader(records),
                            cid=cid,
                            actors=fixed_actors,
                            trace=snapshot["trace"],
                            progress=progress,
                            cancel=token,
                            rejected=rejected,
                        )
                except Cancelled:
                    # 领域事务已回滚；取消不是丢失收尾责任的持久失败。
                    return None
                except Exception as exc:
                    if not decision.rolled_back:
                        raise
                    # 已有 attempt / 模型审计不变；只携回已核实回滚的原异常。
                    rolled_back_error.append(exc)
                    return None

            with storage_cancel_scope(token):
                result = await persist(
                    commit,
                    items,
                    window,
                    list(reader.rows),
                    dict(actors),
                    dict(actors.reply_roles),
                    end,
                    rejected,
                    scene=conversation.scene,
                    conversation_id=cid,
                )
            if rolled_back_error:
                raise rolled_back_error[0]
            return result
