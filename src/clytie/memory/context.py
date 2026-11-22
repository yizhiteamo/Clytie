"""本体记忆上下文与当场记工具；调用内编号不暴露持久记录编号。"""

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from clytie.backup.coordination import run_sync
from clytie.sdk import Cancelled
from clytie.sdk.context import RevisionConflict
from clytie.sdk.types import ChatMessage, TextPart, ToolCall, ToolResult, ToolSpec
from clytie.storage.owner import (
    StorageBusy,
    StorageCancelled,
    StorageFailed,
    cancellable_storage_commit,
    storage_cancel_scope,
)

from .fade import reinforce
from .origins import scope as origin_scope
from .retrieval import Retrieval
from .store import query_tokens, tokens


@dataclass
class Session:
    references: dict = field(default_factory=dict)
    writes: int = 0
    recalls: int = 0


def normalize_leak(text):
    return "".join(
        c
        for c in unicodedata.normalize("NFKC", text).lower()
        if not c.isspace()
        and unicodedata.category(c) != "Cf"
        and not unicodedata.category(c).startswith("P")
    )


class MemoryContext:
    def __init__(self, store, *, log, ledger=None, retrieval=None, changed=None):
        self.store, self.log, self.ledger = store, log, ledger
        self.retrieval = retrieval or Retrieval(store, log=log)
        self.changed = changed
        self.sessions = {}
        self.core_overflow = 0

    def session(self, trace):
        return self.sessions.setdefault(trace, Session())

    def specifications(self, actor):
        result = [
            ToolSpec(
                name="core_recall",
                description="回想记忆和当前会话的任务记录；没有找到就如实说不记得。",
                when_not_to_use="每次回复最多三次",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            )
        ]
        if actor is not None and actor.role == "owner":
            result.extend(
                [
                    ToolSpec(
                        name="core_remember",
                        description="记住主人亲口说的事实、偏好和经历；明确要求记住时 explicit=true。",
                        when_not_to_use="第三方说法、截图、猜测或玩笑；不是主人亲口说的内容不记。主人说不用记、别记这个时不记；已经在本次记下的，用 core_forget 撤销。",
                        parameters={
                            "type": "object",
                            "properties": {
                                "kind": {"enum": ["fact", "preference", "event"]},
                                "content": {"type": "string", "maxLength": 300},
                                "visibility": {
                                    "enum": ["private", "public", "relationship"],
                                    "default": "private",
                                },
                                "explicit": {"type": "boolean", "default": False},
                                "replaces": {"type": "string", "pattern": r"^\[记忆[1-9][0-9]*\]$"},
                            },
                            "required": ["kind", "content"],
                            "additionalProperties": False,
                        },
                    ),
                    ToolSpec(
                        name="core_forget",
                        description="主人说别再提了、那个记错了，删掉时，撤销本次想起的一条记忆。主人说不用记、别记这个时不记；已经在本次记下的，用 core_forget 撤销。",
                        when_not_to_use="没有当前调用中的记忆编号或没有主人要求",
                        parameters={
                            "type": "object",
                            "properties": {
                                "target": {"type": "string", "pattern": r"^\[记忆[1-9][0-9]*\]$"},
                                "reason": {"type": "string", "minLength": 1},
                            },
                            "required": ["target", "reason"],
                            "additionalProperties": False,
                        },
                    ),
                ]
            )
        return result

    def label(self, record, session):
        days = max(0, (self.store.clock.now() - record.created_at).days)
        when = (
            "今天"
            if days == 0
            else "昨天"
            if days == 1
            else "这几天"
            if days < 7
            else "上周"
            if days < 14
            else f"{days // 7}周前"
            if days < 60
            else f"{days // 30}个月前"
        )
        label = next(
            (key for key, (rid, _) in session.references.items() if rid == record.record_id),
            f"[记忆{len(session.references) + 1}]",
        )
        session.references[label] = (record.record_id, record.revision)
        suffix = "很久以前的事，记不太清了" if record.decay.frozen else ""
        return f"{label} {record.content}（{when}）{suffix}"

    async def build(self, trace, conversation, query, *, use=None):
        session = self.session(trace)
        identity = self.retrieval.embedder_id
        if identity != getattr(self, "_embedding_identity", None):
            self._embedding_identity = identity
            if self.changed:
                self.changed()
        scene, cid = conversation.scene, conversation.conversation_id
        if scene == "stranger_private":
            return []
        try:
            visible = await run_sync(
                self.store, self.store.project, scene=scene, conversation_id=cid
            )
        except (StorageBusy, StorageCancelled, StorageFailed):
            raise
        except Exception:
            self.log.warning("memory.projection_failed")
            return []
        if not visible:
            return [ChatMessage(role="system", parts=[TextPart(text="你们之间还没有记住的事")])]
        config = self.store.config.get("memory", {})
        core = sorted(
            (
                r
                for r in visible
                if r.kind not in {"profile", "jargon"}
                and (
                    r.basis == "owner_confirmed"
                    or r.kind == "preference"
                    and r.basis == "owner_stated"
                )
            ),
            key=lambda r: (r.updated_at, r.record_id),
            reverse=True,
        )
        lines, selected, used = [], set(), 0
        for record in core:
            # 预算包括完整展示行，超出时不分割内容。
            previous = dict(session.references)
            label = self.label(record, session)
            if used + len(label) > config.get("core_chars", 4000):
                session.references = previous
                continue
            lines.append(label)
            selected.add(record.record_id)
            used += len(label)
        self.core_overflow = len(core) - len(selected)
        try:
            recalled = await self.retrieval.search(
                query, scene, cid, limit=config.get("recall_limit", 10) + len(selected)
            )
        except (StorageBusy, StorageCancelled, StorageFailed):
            raise
        except Exception:
            self.log.warning("memory.retrieval_failed")
            recalled = []
        scopes = await run_sync(
            self.store,
            lambda: {
                r.record_id: origin_scope(self.store, r) for r in recalled if r.kind == "jargon"
            },
        )
        used, count = 0, 0
        for record in recalled:
            if record.record_id in selected or count >= config.get("recall_limit", 10):
                continue
            previous = dict(session.references)
            label = self.label(record, session)
            if record.kind == "jargon":
                label += (
                    "（这个群里的说法）"
                    if scopes[record.record_id] == cid
                    else "（群里的说法，听懂就好，不主动用）"
                )
            if used + len(label) > config.get("recall_chars", 3000):
                session.references = previous
                continue
            lines.append(label)
            selected.add(record.record_id)
            used += len(label)
            count += 1
        chosen = [r for r in visible if r.record_id in selected]
        if use is None:
            await run_sync(self.store, reinforce, self.store, chosen)
        else:
            use.memories.update((r.record_id, r.revision) for r in chosen)
        return [ChatMessage(role="system", parts=[TextPart(text="记忆：\n" + "\n".join(lines))])]

    async def reinforce_used(self, revisions):
        def apply():
            records = [self.store.get_raw(key) for key in revisions]
            reinforce(
                self.store,
                [
                    r
                    for r in records
                    if r and not r.retracted and r.revision == revisions[r.record_id]
                ],
            )

        await run_sync(self.store, apply)

    async def guard(self, text, scene):
        try:
            return await run_sync(self.store, self._guard, text, scene=scene)
        except (StorageBusy, StorageCancelled, StorageFailed):
            raise
        except Exception:
            self.log.warning("memory.guard_failed")
            return True

    def _guard(self, text, scene):
        if scene in self.store.config.get("memory", {}).get("private_scenes", ["owner_private"]):
            return False
        value = normalize_leak(text)
        minimum = self.store.config.get("memory", {}).get("leak_min_chars", 8)
        segments = {value[i : i + minimum] for i in range(max(0, len(value) - minimum + 1))}
        if not segments:
            return False
        for record in self.store.all_records():
            if record.visibility != "private" or record.retracted:
                continue
            private_text = normalize_leak(record.content)
            if any(
                private_text[i : i + minimum] in segments
                for i in range(max(0, len(private_text) - minimum + 1))
            ):
                return True
        return False

    async def task_recall(self, query, conversation):
        return await run_sync(self.store, lambda: self._task_recall(query, conversation))

    def _task_recall(self, query, conversation):
        if self.ledger is None:
            return []
        from clytie.kernel.projection import visible

        words = query_tokens(query)
        rows = []
        for record in self.ledger.rows("record"):
            if (
                record["kind"] != "task"
                or record["conversation_id"] != conversation.conversation_id
            ):
                continue
            if not visible(
                record,
                scene=conversation.scene,
                conversation_id=conversation.conversation_id,
                private_scenes=self.store.config.get("memory", {}).get(
                    "private_scenes", ["owner_private"]
                ),
            ):
                continue
            summary = {
                k: record.get(k)
                for k in (
                    "task_id",
                    "tool",
                    "requester",
                    "exec_status",
                    "delivery_status",
                    "finished_at",
                )
            }
            summary["summary"] = [
                {
                    "plan": (i.get("plan") or {}).get("summary", ""),
                    "result": (i.get("result") or {}).get("content", "")[:200],
                }
                for i in self.ledger.rows("invocation")
                if i["trace_id"] == record["trace_id"]
            ]
            display = json.dumps(summary, ensure_ascii=False)
            searchable = " ".join(
                [
                    summary.get("tool") or "",
                    *[f"{item['plan']} {item['result']}" for item in summary["summary"]],
                ]
            )
            if words & tokens(searchable):
                rows.append((datetime.fromisoformat(record["created_at"]), display))
        return [text for _, text in sorted(rows, reverse=True)[:10]]

    async def execute(self, trace, conversation, actor, call, cancel, *, finalization=None):
        session = self.session(trace)
        cancel.raise_if_cancelled()
        rows, failure = [], None
        if call.tool == "core_recall":
            try:
                if session.recalls >= 3:
                    raise ValueError("本次回复的回想次数已用完")
                session.recalls += 1
                rows = await self.retrieval.search(
                    call.arguments["query"],
                    scene=conversation.scene,
                    conversation_id=conversation.conversation_id,
                    limit=10,
                    cancel=cancel,
                    include_frozen=True,
                )
            except (StorageBusy, StorageCancelled, StorageFailed):
                raise
            except Exception as exc:
                failure = exc

        # 同一门闩覆盖记忆提交和账本审计；两个数据库仍各自真实提交。
        async def persist(function, *args, **kwargs):
            if finalization is not None:
                return await finalization.persist(function, *args, **kwargs)
            return await run_sync(self.store, function, *args, **kwargs)

        def execute(trace, conversation, actor, call, rows, references, **_):
            try:
                with cancellable_storage_commit(cancel):
                    return self._execute(
                        trace,
                        conversation,
                        actor,
                        ToolCall.model_validate(call),
                        cancel,
                        rows,
                        failure,
                        references,
                    )
            except Cancelled:
                return None

        with storage_cancel_scope(cancel):
            result = await persist(
                execute,
                trace,
                conversation,
                actor,
                call.model_dump(mode="json"),
                rows,
                dict(session.references),
                scene=conversation.scene,
                conversation_id=conversation.conversation_id,
            )
        if result is None:
            cancel.raise_if_cancelled()
        if result.status == "ok" and call.tool != "core_recall" and self.changed:
            self.changed()
        return result

    def _execute(self, trace, conversation, actor, call, cancel, rows, failure, references):
        session = self.session(trace)
        cancel.raise_if_cancelled()
        args = dict(call.arguments)
        scope = dict(scene=conversation.scene, conversation_id=conversation.conversation_id)
        try:
            if failure is not None:
                raise failure
            if call.tool == "core_recall":
                lines = [self.label(r, session) for r in rows]
                reinforce(self.store, rows)
                lines.extend(self._task_recall(args["query"], conversation))
                content = "\n".join(lines) or "记忆和任务记录里都没有找到相关的事"
            else:
                if actor is None or actor.role != "owner":
                    raise ValueError("只有主人可以使用这个记忆工具")
                if session.writes >= self.store.config.get("memory", {}).get(
                    "max_writes_per_record", 5
                ):
                    raise ValueError("本次回复的记忆写入次数已用完")
                session.writes += 1
                reference = (
                    args.get("replaces") if call.tool == "core_remember" else args.get("target")
                )
                if reference is not None:
                    if (
                        not re.fullmatch(r"\[记忆[1-9][0-9]*\]", reference)
                        or reference not in references
                    ):
                        raise KeyError("找不到这条记忆")
                    key, revision = references[reference]
                    changes = (
                        {"retracted": True}
                        if call.tool == "core_forget"
                        else {
                            "kind": args["kind"],
                            "content": args["content"],
                            "visibility": args.get("visibility", "private"),
                        }
                    )
                    original = self.store.get_raw(key)
                    if (
                        call.tool == "core_remember"
                        and original is not None
                        and original.kind in {"profile", "jargon"}
                    ):
                        changes["kind"] = original.kind
                        if "visibility" not in args:
                            changes["visibility"] = original.visibility
                    self.store.revise(
                        key,
                        revision,
                        changes,
                        **scope,
                        operator="owner",
                        explicit=args.get("explicit", False),
                        trace=trace,
                        reason=args.get("reason"),
                    )
                else:
                    self.store.write(
                        {
                            "kind": args["kind"],
                            "content": args["content"],
                            "visibility": args.get("visibility", "private"),
                        },
                        **scope,
                        owner=actor.actor_id,
                        operator="owner",
                        explicit=args.get("explicit", False),
                        trace=trace,
                    )
                content = "记下了" if call.tool == "core_remember" else "已经不会再提了"
            status, error = "ok", None
        except (StorageBusy, StorageCancelled, StorageFailed):
            raise
        except RevisionConflict:
            self.log.warning("memory.revision_conflict", tool=call.tool)
            content, status, error = (
                "记忆刚被改过，请重新回想",
                "error",
                {"code": "revision_conflict", "message": ""},
            )
        except KeyError:
            content, status, error = "找不到这条记忆", "error", {"code": "not_found", "message": ""}
        except ValueError as exc:
            content, status, error = str(exc), "error", {"code": "memory_limit", "message": ""}
        except Exception:
            self.log.warning("memory.operation_failed", tool=call.tool)
            content, status, error = (
                "暂时想不起来，请稍后再试"
                if call.tool == "core_recall"
                else "这次没有记住，请稍后再试",
                "error",
                {"code": "memory_unavailable", "message": ""},
            )
        if self.ledger:
            with self.ledger.transaction():
                self.ledger.audit(
                    "core.tool",
                    trace_id=trace,
                    tool=call.tool,
                    status=status,
                    error=error,
                    arguments={"fields": sorted(args)},
                    result="memory_result",
                )
        return ToolResult(
            call_id=call.call_id, data={}, status=status, content=content, error=error
        )
