"""从同一本账本装配上下文；不向插件开放账本。"""

import json
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from pydantic import TypeAdapter

from clytie.backup.coordination import run_sync
from clytie.kernel.projection import visible
from clytie.sdk.types import Actor, AssetRef, ChatMessage, ContextRequest, MessagePart, TextPart

from .appearance import RULES as APPEARANCE_RULES
from .image_context import Slot
from .materials import Materials
from .policy import actor_label, text_parts

PART = TypeAdapter(MessagePart)


def message(role, text):
    return ChatMessage(role=role, parts=[TextPart(text=text)])


def history_time(value):
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return instant.astimezone(timezone.utc).isoformat(timespec="microseconds")


class HistoryMessage(NamedTuple):
    at: str
    role: str
    sender: Actor | None
    key: str
    prefix: str
    parts: list
    ref: dict | None


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

    def image_offsets(self, parts, media, key, at, rank, prefix, outgoing=False):
        offsets = []
        if not self.agent.features.supports_images:
            return offsets
        pos = len(prefix)
        for i, part in enumerate(parts):
            row = media.get((key, i))
            if part.type == "image" and (row or outgoing and part.asset):
                asset = (
                    part.asset
                    if outgoing
                    else AssetRef.model_validate(row["asset"])
                    if row.get("asset")
                    else None
                )
                offsets.append(
                    (
                        pos,
                        Slot(
                            (key, i),
                            at,
                            rank,
                            "ok" if outgoing else row["status"],
                            asset,
                            bool(row and row.get("first_frame")),
                            outgoing,
                        ),
                    )
                )
            pos += len(self.render_parts([part])) + 1
        return offsets

    async def quote(
        self, ref, conversation, authors=None, *, ledger=None, image_info=None, render_parts=None
    ):
        render_parts = render_parts or self.render_parts
        if ledger is None:
            tables = await run_sync(self.agent.ledger, self._read_tables)
            ledger = LedgerSnapshot(tables)
        for row in ledger.rows("inbound"):
            if row.get("message") and row["message"]["ref"] == ref.model_dump(mode="json"):
                if row.get("reason") != "boundary_paused" and self.visible(row, conversation):
                    if authors is not None:
                        authors.append(Actor.model_validate(row["message"]["sender"]))
                    parts = [PART.validate_python(p) for p in row["message"]["parts"] or []]
                    if image_info:
                        offsets, media, prefix = image_info
                        offsets.extend(
                            self.image_offsets(
                                parts, media, row["event_id"], row["received_at"], 1, prefix
                            )
                        )
                    return render_parts(parts)
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
                parts = [PART.validate_python(p) for p in row.get("parts") or []]
                if image_info:
                    offsets, media, prefix = image_info
                    offsets.extend(
                        self.image_offsets(
                            parts,
                            media,
                            row["intent_id"],
                            row.get("created_at", record["created_at"]),
                            1,
                            prefix,
                            outgoing=True,
                        )
                    )
                return render_parts(parts)
        return "引用了一条看不到的消息"

    def history_rows(self, ledger, conversation, current_ids):
        """Shared projection for the reply and attention models, before either applies its budget."""
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
            msg = row["message"]
            sender = Actor.model_validate(msg["sender"])
            yield HistoryMessage(
                history_time(msg["sent_at"] if row.get("historical") else row["received_at"]),
                "user",
                sender,
                row["event_id"],
                f"{sender.role} {actor_label(sender)}：",
                msg["parts"] or [],
                msg.get("ref"),
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
            yield HistoryMessage(
                history_time(row.get("created_at", record["created_at"])),
                "assistant",
                None,
                row["intent_id"],
                "[不确定是否发出]" if row["status"] == "unknown" else "",
                row.get("parts") or [],
                None,
            )

    def _read_tables(self):
        return {
            table: self.agent.ledger.rows(table)
            for table in (
                "record",
                "invocation",
                "intent",
                "audit",
                "inbound",
                "inbound_media",
                "attempt",
            )
        }

    def _snapshot(self, conversation, preparing):
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
            pending = set()
            for group, rows in groups.items():
                if (
                    group not in announced
                    and any(r["status"] in {"sent", "unknown"} for r in rows)
                    and not all(r["status"] == "sent" for r in rows)
                    and not any(r["status"] in {"pending", "sending", "retry_wait"} for r in rows)
                ):
                    if preparing:
                        pending.add(group)
                    else:
                        ledger.audit("partial_group_context", group_id=group)
            return tables, pending

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
        images=None,
        materials=None,
        use=None,
        available_tools=(),
        catch_up=None,
    ):
        agent = self.agent
        tables, pending = await run_sync(
            agent.ledger, self._snapshot, conversation, use is not None
        )
        if use is not None:
            use.partial_groups.update(pending)
        ledger = LedgerSnapshot(tables)
        media = {(row["record_id"], row["part_index"]): row for row in ledger.rows("inbound_media")}
        if not agent.features.supports_images:
            capabilities += "\n当前模型看不到图片"
        persona = agent.persona.render(conversation.scene)
        if agent.appearance.description:
            persona += "\n当前形象描述：\n" + agent.appearance.description
        if agent.appearance.description or agent.appearance.references:
            persona += "\n无论人设或形象描述怎样写，认图都遵守以下规则。\n" + APPEARANCE_RULES
        result = [
            message("system", persona),
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
            if row.get("proactive"):
                summaries[-1]["origin"] = "proactive"
            summaries[-1]["summary"] = [
                {
                    "plan": (i.get("plan") or {}).get("summary", ""),
                    "result": (i.get("result") or {}).get("content", "")[:200],
                }
                for i in invocations
                if not (i.get("result") or {}).get("material")
            ]
        result.append(message("system", "最近任务：" + json.dumps(summaries, ensure_ascii=False)))
        reference = (materials or Materials()).render(
            ledger,
            conversation,
            self,
            agent.clock.now(),
            agent.config.get("context", {}),
            memory_trace,
        )
        if reference:
            result.append(message("user", reference))
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
            ContextRequest(
                scene=conversation.scene,
                actor=actor,
                budget_chars=budget,
                conversation=conversation,
                has_appearance=bool(agent.appearance.description)
                or any(ref.state == "current" for ref in agent.appearance.references),
            ),
            conversation_id=conversation.conversation_id,
            available_tools=available_tools,
        )
        if mode:
            result.append(message("system", "当前模式：" + mode.name))
        result.extend(message("system", s.title + "\n" + s.text) for s in sections)
        history = []
        visible_sources = {}
        for item in self.history_rows(ledger, conversation, {e.event_id for e in events}):
            parts = [PART.validate_python(p) for p in item.parts]
            if catch_up and item.role == "user":
                visible_sources[item.key] = (item.sender, item.prefix + self.render_parts(parts))
            history.append(
                (
                    item.at,
                    item.role,
                    item.prefix + self.render_parts(parts),
                    item.sender,
                    item.prefix + text_parts(parts),
                    self.image_offsets(
                        parts, media, item.key, item.at, 2, item.prefix, item.role == "assistant"
                    ),
                )
            )
        limits = agent.config.get("context", {})
        memory_query = "\n".join(
            [text_parts(e.message.parts) for e in events if e.message]
            + [query for _, _, _, _, query, _ in sorted(history, key=lambda r: r[0])[-3:]]
        )
        if agent.memory_context:
            memory_messages = await agent.memory_context(conversation, actor)
        else:
            memory_messages = await agent.memory.build(
                memory_trace or "context",
                conversation,
                memory_query,
                **({"use": use} if use is not None else {}),
            )
        result[memory_position:memory_position] = memory_messages
        remaining, kept = limits.get("max_chars", 20_000), []
        for _, role, content, sender, _, offsets in sorted(history, key=lambda r: r[0])[
            -limits.get("max_messages", 100) :
        ][::-1]:
            content = content[: limits.get("max_single_message_chars", 2000)]
            if len(content) > remaining:
                break
            history_msg = message(role, content)
            if images:
                images.register(
                    history_msg,
                    [(pos, s) for pos, s in offsets if pos + len("[图片]") <= len(content)],
                )
            kept.append((history_msg, sender))
            remaining -= len(content)
        reminder_position = len(result)
        authors = [sender for _, sender in reversed(kept) if sender is not None]
        result.extend(msg for msg, _ in reversed(kept))
        if catch_up:
            result.append(message("system", catch_up.instructions))
            cutoff = sorted(history, key=lambda row: row[0])[-len(kept)][0] if kept else None
            for sender, earlier in catch_up.prior_context(events, cutoff, visible_sources):
                authors.append(sender)
                result.append(
                    message(
                        "user",
                        "当时在聊的：" + earlier[: limits.get("max_single_message_chars", 2000)],
                    )
                )
        for event in events:
            msg = event.message
            prefix = f"{msg.sender.role} {actor_label(msg.sender)}："
            if catch_up:
                local_time = msg.sent_at.astimezone(agent.clock.local_tz)
                prefix = f"[{local_time:%Y-%m-%d %H:%M:%S}] " + prefix
            text = prefix + self.render_parts(msg.parts)
            offsets = self.image_offsets(
                msg.parts,
                media,
                event.event_id,
                event.received_at.isoformat(),
                0,
                prefix,
            )
            if msg.reply_to:
                text += "\n引用：" + await self.quote(
                    msg.reply_to,
                    conversation,
                    authors,
                    ledger=ledger,
                    image_info=(offsets, media, text + "\n引用："),
                )
            authors.append(msg.sender)
            result.append(message("user", text))
            if images:
                images.register(result[-1], offsets)
        reminder = agent.boundary.reminder(authors)
        if reminder:
            result.insert(reminder_position, message("system", reminder))
        if extra:
            result.append(message("system" if proactive else "user", extra))
        internal_scenes = agent.config.get("names", {}).get("internal_scenes", ["owner_private"])
        if conversation.scene not in internal_scenes and any(
            event.addressed and event.addressed.name == "internal" for event in events
        ):
            result.append(message("system", "这条消息用你的内部名字叫你。在这里不要说出这个名字。"))
        return result
