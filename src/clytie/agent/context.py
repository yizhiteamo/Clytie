"""从同一本账本装配上下文；不向插件开放账本。"""

import json
from datetime import datetime, timedelta

from pydantic import TypeAdapter

from clytie.backup.coordination import transaction
from clytie.kernel.projection import visible
from clytie.sdk.types import Actor, ChatMessage, ContextRequest, MessagePart, TextPart

from .policy import actor_label, text_parts

PART = TypeAdapter(MessagePart)


def message(role, text):
    return ChatMessage(role=role, parts=[TextPart(text=text)])


class Context:
    def __init__(self, agent):
        self.agent = agent

    def visible(self, row, conversation):
        return visible(
            row,
            scene=conversation.scene,
            conversation_id=conversation.conversation_id,
            private_scenes=self.agent.config.get("memory", {}).get(
                "private_scenes", ["owner_private"]
            ),
        )

    def quote(self, ref, conversation, authors=None):
        ledger = self.agent.ledger
        for row in ledger.rows("inbound"):
            if row.get("message") and row["message"]["ref"] == ref.model_dump(mode="json"):
                if row.get("reason") != "boundary_paused" and self.visible(row, conversation):
                    if authors is not None:
                        authors.append(Actor.model_validate(row["message"]["sender"]))
                    return text_parts(
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
                return text_parts([PART.validate_python(p) for p in row.get("parts") or []])
        return "引用了一条看不到的消息"

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
        agent, ledger = self.agent, self.agent.ledger
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
            async with transaction(ledger):
                ledger.audit("partial_group_context", group_id=group)
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
            content = text_parts([PART.validate_python(p) for p in row["message"]["parts"] or []])
            history.append(
                (
                    row["received_at"],
                    "user",
                    f"{sender.role} {actor_label(sender)}：{content}",
                    sender,
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
            content = text_parts([PART.validate_python(p) for p in row.get("parts") or []])
            history.append(
                (
                    row.get("created_at", record["created_at"]),
                    "assistant",
                    ("[不确定是否发出]" if row["status"] == "unknown" else "") + content,
                    None,
                )
            )
        limits = agent.config.get("context", {})
        memory_query = "\n".join(
            [text_parts(e.message.parts) for e in events if e.message]
            + [text for _, _, text, _ in sorted(history, key=lambda r: r[0])[-3:]]
        )
        if agent.memory_context:
            memory_messages = await agent.memory_context(conversation, actor)
        else:
            memory_messages = await agent.memory.build(
                memory_trace or "context", conversation, memory_query
            )
        result[memory_position:memory_position] = memory_messages
        remaining, kept = limits.get("max_chars", 20_000), []
        for _, role, content, sender in sorted(history, key=lambda r: r[0])[
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
            text = f"{msg.sender.role} {actor_label(msg.sender)}：" + text_parts(msg.parts)
            if msg.reply_to:
                text += "\n引用：" + self.quote(msg.reply_to, conversation, authors)
            authors.append(msg.sender)
            result.append(message("user", text))
        reminder = agent.boundary.reminder(authors)
        if reminder:
            result.insert(reminder_position, message("system", reminder))
        if extra:
            result.append(message("system" if proactive else "user", extra))
        return result
