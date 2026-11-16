"""从同一本账本装配上下文；不向插件开放账本。"""

import json
from datetime import datetime, timedelta

from pydantic import TypeAdapter

from clytie.backup.coordination import run_sync
from clytie.kernel.projection import visible
from clytie.sdk.types import Actor, ChatMessage, ContextRequest, MessagePart, TextPart

from .policy import actor_label, text_parts

PART = TypeAdapter(MessagePart)


def message(role, text):
    return ChatMessage(role=role, parts=[TextPart(text=text)])


class LedgerSnapshot:
    def __init__(self, tables):
        self.tables = tables

    def rows(self, table):
        return self.tables[table]

    def get(self, table, key):
        field = {"record": "trace_id", "intent": "intent_id"}[table]
        return next((row for row in self.tables[table] if row[field] == key), None)


class Context:
    def __init__(self, agent):
        self.agent = agent

    def render_parts(self, parts):
        """Model context only; policy and retrieval retain text_parts semantics."""
        rendered = []
        labels = {
            "image": "[图片]",
            "audio": "[语音]",
            "video": "[视频]",
            "file": "[文件]",
            "quote": "[引用]",
            "unknown": "[不支持的消息]",
        }
        people = self.agent.instance.get("people", {})
        for part in parts:
            if part.type == "mention":
                label = (
                    "你"
                    if part.is_self
                    else (
                        "主人" if people.get(part.actor_id, {}).get("role") == "owner" else "某人"
                    )
                )
                rendered.append(f"[@{label}]")
            elif part.type == "sticker":
                rendered.append(f"[表情：{part.summary}]" if part.summary else "[表情]")
            elif part.type in labels:
                rendered.append(labels[part.type])
            else:
                rendered.append(text_parts([part]))
        return "\n".join(rendered)

    def visible(self, row, conversation):
        return visible(
            row,
            scene=conversation.scene,
            conversation_id=conversation.conversation_id,
            private_scenes=self.agent.config.get("memory", {}).get(
                "private_scenes", ["owner_private"]
            ),
        )

    async def quote(self, ref, conversation, authors=None, *, ledger=None):
        if ledger is None:
            tables = await run_sync(self.agent.ledger, self._read_tables)
            ledger = LedgerSnapshot(tables)
        for row in ledger.rows("inbound"):
            if row.get("message") and row["message"]["ref"] == ref.model_dump(mode="json"):
                if row.get("reason") != "boundary_paused" and self.visible(row, conversation):
                    if authors is not None:
                        authors.append(Actor.model_validate(row["message"]["sender"]))
                    return self.render_parts(
                        [PART.validate_python(p) for p in row["message"]["parts"] or []]
                    )
                return "引用了一条看不到的消息"
        for attempt in ledger.rows("attempt"):
            receipt = attempt.get("receipt")
            if not receipt or receipt.get("platform_message_id") != ref.platform_message_id:
                continue
            row = ledger.get("intent", attempt["intent_id"])
            record = ledger.get("record", row["trace_id"])
            if (
                row["channel_id"] == ref.channel_id
                and row["conversation_id"] == ref.conversation_id
                and self.visible(record, conversation)
            ):
                return self.render_parts([PART.validate_python(p) for p in row.get("parts") or []])
        return "引用了一条看不到的消息"

    def _read_tables(self):
        return {
            table: self.agent.ledger.rows(table)
            for table in ("record", "invocation", "intent", "audit", "inbound", "attempt")
        }

    def _snapshot(self, conversation):
        ledger = self.agent.ledger
        with ledger.transaction():
            tables = self._read_tables()
            snapshot = LedgerSnapshot(tables)
            groups = {}
            for row in snapshot.rows("intent"):
                if row.get("split") and row["conversation_id"] == conversation.conversation_id:
                    record = snapshot.get("record", row["trace_id"])
                    if self.visible(record, conversation):
                        groups.setdefault(row["split"]["group_id"], []).append(row)
            announced = {
                a.get("group_id")
                for a in snapshot.rows("audit")
                if a["kind"] == "partial_group_context"
            }
            for group, rows in groups.items():
                if (
                    group not in announced
                    and any(r["status"] in {"sent", "unknown"} for r in rows)
                    and not all(r["status"] == "sent" for r in rows)
                    and not any(r["status"] in {"pending", "sending", "retry_wait"} for r in rows)
                ):
                    ledger.audit("partial_group_context", group_id=group)
            return tables

    async def build(
        self,
        conversation,
        actor,
        events,
        capabilities,
        *,
        mode=None,
        extra="",
        proactive=False,
        memory_trace=None,
    ):
        agent = self.agent
        ledger = LedgerSnapshot(await run_sync(agent.ledger, self._snapshot, conversation))
        result = [
            message("system", agent.persona.render(conversation.scene)),
            message("system", "能力状态：\n" + capabilities),
        ]
        cutoff = agent.clock.now() - timedelta(hours=24)
        records = [
            r
            for r in ledger.rows("record")
            if r["kind"] == "task"
            and r["conversation_id"] == conversation.conversation_id
            and datetime.fromisoformat(r["created_at"]) >= cutoff
            and self.visible(r, conversation)
        ]
        summaries = []
        for row in records[-10:]:
            invocations = [i for i in ledger.rows("invocation") if i["trace_id"] == row["trace_id"]]
            summaries.append(
                {
                    k: row.get(k)
                    for k in (
                        "task_id",
                        "tool",
                        "requester",
                        "exec_status",
                        "delivery_status",
                        "finished_at",
                    )
                }
            )
            summaries[-1]["summary"] = [
                {
                    "plan": (i.get("plan") or {}).get("summary", ""),
                    "result": (i.get("result") or {}).get("content", "")[:200],
                }
                for i in invocations
            ]
        result.append(message("system", "最近任务：" + json.dumps(summaries, ensure_ascii=False)))
        groups = {}
        for row in ledger.rows("intent"):
            if row.get("split") and row["conversation_id"] == conversation.conversation_id:
                record = ledger.get("record", row["trace_id"])
                if self.visible(record, conversation):
                    groups.setdefault(row["split"]["group_id"], []).append(row)
        announced = {
            a.get("group_id") for a in ledger.rows("audit") if a["kind"] == "partial_group_context"
        }
        for group, rows in groups.items():
            if (
                group in announced
                or not any(r["status"] in {"sent", "unknown"} for r in rows)
                or all(r["status"] == "sent" for r in rows)
            ):
                continue
            if any(r["status"] in {"pending", "sending", "retry_wait"} for r in rows):
                continue
            parts = [
                f"第 {r['split']['index'] + 1} 段："
                + {"sent": "确定发出", "unknown": "不确定是否发出", "failed": "没有发出"}[
                    r["status"]
                ]
                for r in sorted(rows, key=lambda x: x["split"]["index"])
            ]
            result.append(message("system", "上次拆分消息：" + "；".join(parts)))
        memory_position = len(result)
        budget = agent.config.get("context", {}).get("material_budget_chars", 6000)
        sections = await agent.host.context_sections(
            agent.cid,
            ContextRequest(scene=conversation.scene, actor=actor, budget_chars=budget),
            conversation_id=conversation.conversation_id,
        )
        if mode:
            result.append(message("system", "当前模式：" + mode.name))
        result.extend(message("system", s.title + "\n" + s.text) for s in sections)
        history = []
        current_ids = {e.event_id for e in events}
        for row in ledger.rows("inbound"):
            if (
                row["event_id"] in current_ids
                or row["conversation_id"] != conversation.conversation_id
                or row["state"] not in {"processing", "ignored", "merged"}
                or row.get("reason") == "boundary_paused"
                or not row.get("message")
                or not self.visible(row, conversation)
            ):
                continue
            sender = Actor.model_validate(row["message"]["sender"])
            parts = [PART.validate_python(p) for p in row["message"]["parts"] or []]
            content = self.render_parts(parts)
            history.append(
                (
                    row["received_at"],
                    "user",
                    f"{sender.role} {actor_label(sender)}：{content}",
                    sender,
                    f"{sender.role} {actor_label(sender)}：{text_parts(parts)}",
                )
            )
        for row in ledger.rows("intent"):
            if (
                row["conversation_id"] != conversation.conversation_id
                or row["status"] not in {"sent", "unknown"}
                or row["status_reaction"]
            ):
                continue
            record = ledger.get("record", row["trace_id"])
            if not self.visible(record, conversation):
                continue
            parts = [PART.validate_python(p) for p in row.get("parts") or []]
            content = self.render_parts(parts)
            history.append(
                (
                    row.get("created_at", record["created_at"]),
                    "assistant",
                    ("[不确定是否发出]" if row["status"] == "unknown" else "") + content,
                    None,
                    ("[不确定是否发出]" if row["status"] == "unknown" else "") + text_parts(parts),
                )
            )
        limits = agent.config.get("context", {})
        memory_query = "\n".join(
            [text_parts(e.message.parts) for e in events if e.message]
            + [query for _, _, _, _, query in sorted(history, key=lambda r: r[0])[-3:]]
        )
        if agent.memory_context:
            memory_messages = await agent.memory_context(conversation, actor)
        else:
            memory_messages = await agent.memory.build(
                memory_trace or "context", conversation, memory_query
            )
        result[memory_position:memory_position] = memory_messages
        remaining, kept = limits.get("max_chars", 20_000), []
        for _, role, content, sender, _ in sorted(history, key=lambda r: r[0])[
            -limits.get("max_messages", 100) :
        ][::-1]:
            content = content[: limits.get("max_single_message_chars", 2000)]
            if len(content) > remaining:
                break
            kept.append((message(role, content), sender))
            remaining -= len(content)
        reminder_position = len(result)
        authors = [sender for _, sender in reversed(kept) if sender is not None]
        result.extend(msg for msg, _ in reversed(kept))
        for event in events:
            msg = event.message
            text = f"{msg.sender.role} {actor_label(msg.sender)}：" + self.render_parts(msg.parts)
            if msg.reply_to:
                text += "\n引用：" + await self.quote(
                    msg.reply_to, conversation, authors, ledger=ledger
                )
            authors.append(msg.sender)
            result.append(message("user", text))
        reminder = agent.boundary.reminder(authors)
        if reminder:
            result.insert(reminder_position, message("system", reminder))
        if extra:
            result.append(message("system" if proactive else "user", extra))
        return result
