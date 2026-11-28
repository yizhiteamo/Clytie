"""Controlled, text-only model judgments for a pending conversation batch."""

import asyncio
import json
import re
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from clytie.backup.coordination import run_sync
from clytie.kernel.delivery import OwnedFinalization
from clytie.sdk import CancelToken
from clytie.sdk.behavior import JudgeFacts, Judgment
from clytie.sdk.types import Budget, EventEnvelope, MessageRef, ModelRequest

from .context import PART, LedgerSnapshot, message
from .policy import actor_label
from .safety_belt import UNREGISTERED_USAGE

JOIN_FORMAT = (
    "只返回 JSON 对象。join 和 new_topic 是布尔值，"
    "分别表示想接话和换了话题。focus 填你想接的那条消息的序号，没有就填 null。"
)
JOIN_PROMPT = (
    "你看到群里这些消息，像平常一样想想要不要接话。话题与你无关、已经聊累了，"
    "或者没有想说的话，都可以不接。" + JOIN_FORMAT
)
FOLLOW_UP_PROMPT = (
    "你刚在群里说过话。看看大家是在接你的话，还是在和别人聊。"
    "对方在跟你说，或者你对这个话题有自己的看法，就自然地接话。"
    "如果他们在和别人聊，你也没什么想说的，就让他们继续。" + JOIN_FORMAT
)
RECHECK_PROMPT = (
    "看一下刚来的消息和你写好的回复，现在还要发这段回复吗？只返回 JSON 对象，send 是布尔值。"
)


class JoinAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    join: bool
    focus: int | None
    new_topic: bool
    calling_me: bool | None = None


class SendAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    send: bool


class StaleAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    respond: bool
    calling_me: bool | None = None


class AttentionJudgments:
    def __init__(self, agent):
        self.agent = agent

    @property
    def config(self):
        return self.agent.config.get("attention", {}).get("judge", {})

    def _snapshot(self, conversation, messages, limit):
        """Only read recent history in this conversation and the batch's explicit quote targets."""
        ledger = self.agent.ledger
        db = ledger.db
        cid = conversation.conversation_id
        current = json.dumps([f"{m.ref.channel_id}:{m.ref.platform_message_id}" for m in messages])
        inbound = [
            json.loads(row[0])
            for row in db.execute(
                """SELECT data FROM inbound WHERE json_extract(data,'$.conversation_id')=?
                   AND json_extract(data,'$.state') IN ('processing','ignored','merged')
                   AND json_extract(data,'$.reason') IS NOT 'boundary_paused'
                   AND json_extract(data,'$.message') IS NOT NULL
                   AND id NOT IN (SELECT value FROM json_each(?))
                   ORDER BY rtrim(json_extract(data,'$.received_at'),'Z') DESC, rowid DESC LIMIT ?""",
                (cid, current, limit),
            )
        ][::-1]
        intents = {
            key: json.loads(data)
            for key, data in db.execute(
                """SELECT id, data FROM intent WHERE json_extract(data,'$.conversation_id')=?
                   AND json_extract(data,'$.status') IN ('sent','unknown')
                   AND NOT json_extract(data,'$.status_reaction')
                   ORDER BY json_extract(data,'$.created_at') DESC, rowid DESC LIMIT ?""",
                (cid, limit),
            ).fetchall()[::-1]
        }
        quotes = [msg.reply_to for msg in messages if msg.reply_to]
        known = {row["event_id"] for row in inbound}
        for ref in quotes:
            event_id = f"{ref.channel_id}:{ref.platform_message_id}"
            if event_id in known:
                continue
            row = ledger.get("inbound", event_id)
            if row:
                known.add(event_id)
                inbound.append(row)
        attempts = []
        platform_ids = json.dumps([ref.platform_message_id for ref in quotes])
        for data in db.execute(
            """SELECT data FROM attempt WHERE json_extract(data,'$.intent_id')
               IN (SELECT value FROM json_each(?)) OR json_extract(data,'$.receipt.platform_message_id')
               IN (SELECT value FROM json_each(?)) ORDER BY rowid""",
            (json.dumps(list(intents)), platform_ids),
        ):
            attempt = json.loads(data[0])
            key = attempt["intent_id"]
            if key not in intents:
                intents[key] = ledger.get("intent", key)
            attempts.append(attempt)
        records = [
            ledger.get("record", trace) for trace in {r["trace_id"] for r in intents.values()}
        ]
        return {
            "inbound": inbound,
            "intent": list(intents.values()),
            "attempt": attempts,
            "record": records,
        }

    def _render(self, parts):
        # Media are identified within their numbered message. Never open an asset or use raw_ref.
        labels = {"image": "图片", "sticker": "表情"}
        return "\n".join(
            f"[{labels[part.type]}{i}]"
            if part.type in labels
            else self.agent.context.render_parts([part])
            for i, part in enumerate(parts, 1)
        )

    async def _chat(self, conversation, events, *, history, finalization, after_batch=None):
        messages = [e.message if isinstance(e, EventEnvelope) else e for e in events]
        messages = [
            m
            for m in messages
            if m
            and m.ref.channel_id == conversation.channel_id
            and m.ref.conversation_id == conversation.conversation_id
        ]
        count = self.config.get("messages", 20) if history else 0
        if count or any(msg.reply_to for msg in messages):
            args = (self._snapshot, conversation, messages, count)
            tables = (
                await finalization.read(*args)
                if finalization
                else await run_sync(self.agent.ledger, *args)
            )
        else:
            tables = {"inbound": [], "intent": [], "record": [], "attempt": []}
        ledger = LedgerSnapshot(tables)
        context = self.agent.context
        current_ids = {f"{m.ref.channel_id}:{m.ref.platform_message_id}" for m in messages}
        recent = sorted(
            context.history_rows(ledger, conversation, current_ids),
            key=lambda row: row.at.removesuffix("Z").removesuffix("+00:00"),
        )
        receipts = {
            a["intent_id"]: a["receipt"]["platform_message_id"]
            for a in tables["attempt"]
            if a["status"] == "sent" and (a.get("receipt") or {}).get("platform_message_id")
        }
        kept = []
        remaining = 3000
        for item in reversed(recent[-count:] if count else []):
            # Reserve space for the numeric label before assigning final chronological indices.
            room = remaining - len(f"[{count}] ")
            if room <= 0:
                break
            later = after_batch is not None and datetime.fromisoformat(item.at) > after_batch
            body = (
                ("本批之后 " if later else "")
                + item.prefix
                + self._render([PART.validate_python(p) for p in item.parts])
            )[:room]
            ref = MessageRef.model_validate(item.ref) if item.ref else None
            if item.role == "assistant" and item.key in receipts:
                row = ledger.get("intent", item.key)
                ref = MessageRef(
                    channel_id=row["channel_id"],
                    conversation_id=row["conversation_id"],
                    platform_message_id=receipts[item.key],
                )
            kept.append((item.role, body, ref))
            remaining = room - len(body)
        rows = list(reversed(kept))
        for msg in messages:
            body = ("本批消息 " if history else "") + (
                f"{msg.sender.role} {actor_label(msg.sender)}：" + self._render(msg.parts)
            )
            if msg.reply_to:
                body += "\n引用：" + await context.quote(
                    msg.reply_to, conversation, ledger=ledger, render_parts=self._render
                )
            rows.append(("user", body, msg.ref))
        refs = {}
        chat = []
        for number, (role, body, ref) in enumerate(rows, 1):
            if (
                ref
                and ref.conversation_id == conversation.conversation_id
                and ref.channel_id == conversation.channel_id
            ):
                refs[number] = ref
            chat.append(message(role, f"[{number}] {body}"))
        return chat, refs

    async def decide(
        self, conversation, events, facts, *, deadline=None, token=None, finalization=None
    ):
        facts = JudgeFacts.model_validate(facts)
        chat, refs = await self._chat(conversation, events, history=True, finalization=finalization)
        prompt = JOIN_PROMPT if facts.follow_up == "none" else FOLLOW_UP_PROMPT
        if facts.name_hit:
            prompt += "calling_me 表示这些消息是不是在叫你。"
        persona = self.agent.persona.scene_text(conversation.scene)[
            : self.config.get("persona_chars", 1500)
        ]
        messages = [message("system", prompt), message("system", persona), *chat]
        yn = {True: "是", False: "否"}
        relationship = {
            "partner": ["这批消息来自你刚回应过的人。"],
            "others": ["这批消息来自群里的其他人。"],
        }
        messages.append(
            message(
                "system",
                "\n".join(
                    (
                        f"最近十分钟有 {facts.participants} 人说话，你的发言比例为 {facts.share:.1%}。",
                        f"和同一人连续来回 {facts.rounds_with_same_person} 轮。对方接话很快：{yn[facts.counterpart_fast]}。",
                        f"疲劳程度：{facts.fatigue}。最近主动说话没人理：{yn[facts.ignored_recently]}。",
                        f"这批消息有主人发言：{yn[facts.owner_spoke]}。这批消息出现你的名字：{yn[facts.name_hit]}。",
                        *relationship.get(facts.follow_up, ()),
                    )
                ),
            )
        )

        def parse(text):
            fenced = re.fullmatch(r"`{3}json[ \t]*\r?\n(.*?)\r?\n`{3}", text.strip(), re.DOTALL)
            answer = JoinAnswer.model_validate_json(fenced[1] if fenced else text)
            if not facts.name_hit:
                answer.calling_me = None
            elif answer.calling_me is None:
                raise ValueError("calling_me")
            if answer.focus is not None and not 1 <= answer.focus <= len(chat):
                raise ValueError("focus")
            result = answer.model_dump()
            return Judgment(status="ok", **(result | {"focus": refs.get(answer.focus)})), result

        result = await self._call(
            conversation, messages, parse, deadline=deadline, token=token, finalization=finalization
        )
        return result if result is not None else Judgment(status="failed")

    async def stale(self, conversation, events, age, finalization):
        chat, _ = await self._chat(
            conversation,
            events,
            history=True,
            finalization=finalization,
            after_batch=max(e.message.sent_at for e in events),
        )
        named = any(e.addressed.name for e in events)
        prompt = (
            f"本批消息距今约 {int(age.total_seconds() // 60)} 分钟。结合之后群里聊的内容，"
            "现在回应还合适吗？话题还在继续吗？对方还在等吗？别人是不是已经答过了？"
            "只返回 JSON 对象，respond 是布尔值。"
            + ("同时用 calling_me 回答是不是在叫你。" if named else "")
        )
        persona = self.agent.persona.scene_text(conversation.scene)[
            : self.config.get("persona_chars", 1500)
        ]

        def parse(text):
            answer = StaleAnswer.model_validate_json(text)
            if named and answer.calling_me is None:
                raise ValueError("calling_me")
            return Judgment(
                status="ok", join=answer.respond, calling_me=answer.calling_me if named else None
            ), answer.model_dump(exclude_none=True)

        result = await self._call(
            conversation,
            [message("system", prompt), message("system", persona), *chat],
            parse,
            deadline=None,
            token=None,
            finalization=finalization,
        )
        return result or Judgment(status="failed")

    async def recheck(
        self, conversation, new_events, draft_text, *, trace=None, token=None, deadline=None
    ):
        finalization = self.agent.kernel.finalizations.get(trace)
        chat, _ = await self._chat(
            conversation, new_events, history=False, finalization=finalization
        )
        messages = [
            message("system", RECHECK_PROMPT),
            *chat,
            message("user", "写好的回复\n" + draft_text),
        ]

        def parse(text):
            answer = SendAnswer.model_validate_json(text)
            return answer.send, answer.model_dump()

        result = await self._call(
            conversation,
            messages,
            parse,
            deadline=deadline,
            trace=trace,
            token=token,
            finalization=finalization,
        )
        return result is not False

    async def _call(
        self, conversation, messages, parse, *, deadline, token, finalization, trace=None
    ):
        agent = self.agent
        clock = agent.clock
        token = token or CancelToken()
        token.raise_if_cancelled()
        started = clock.now()
        started_tick = clock.monotonic()
        limit = started + timedelta(seconds=self.config.get("seconds", 20))
        if deadline is not None:
            limit = min(limit, deadline)
        if limit <= started:
            return None
        request = ModelRequest(
            messages=messages,
            tools=[],
            trace_id=trace or uuid4().hex,
            budget=Budget(deadline=limit, max_tool_rounds=0),
        )
        local_day = (
            started.astimezone(ZoneInfo(agent.instance.get("timezone", "UTC"))).date().isoformat()
        )
        own = finalization is None
        if own:
            finalization = OwnedFinalization(agent.kernel.delivery)

        def write_audit(kind, elapsed_ms, fields):
            with agent.ledger.transaction():
                agent.ledger.audit(
                    kind,
                    trace_id=request.trace_id,
                    purpose="attention",
                    conversation_id=conversation.conversation_id,
                    day=local_day,
                    duration_ms=elapsed_ms,
                    **fields,
                )

        async def audit(kind, **fields):
            elapsed_ms = int((clock.monotonic() - started_tick) * 1000)
            await finalization.persist(write_audit, kind, elapsed_ms, fields)

        async def usage(turn, timing, fields):
            await audit(
                "model.usage",
                plugin=agent.config["model"]["plugin"],
                input_tokens=turn.usage.input_tokens,
                cached_input_tokens=turn.usage.cached_input_tokens,
                output_tokens=turn.usage.output_tokens,
                finish=turn.finish,
                timing=timing,
                **fields,
            )

        child = token.child()
        model = finalization.track(
            asyncio.create_task(
                agent.model_call(
                    "generate",
                    request,
                    child,
                    scene=conversation.scene,
                    conversation_id=conversation.conversation_id,
                )
            )
        )
        timer = asyncio.create_task(clock.sleep_until(limit))
        cancelled = asyncio.create_task(token.wait())
        observed = False

        async def late():
            if not model.done():
                grace = asyncio.create_task(clock.sleep(agent.kernel.grace))
                try:
                    await asyncio.wait((model, grace), return_when=asyncio.FIRST_COMPLETED)
                    if not model.done():
                        model.cancel()
                finally:
                    grace.cancel()
                    await asyncio.gather(grace, return_exceptions=True)
            try:
                turn = await model
            except (Exception, asyncio.CancelledError):
                return
            await usage(turn, "late", {"status": "failed"})

        try:
            await asyncio.wait((model, timer, cancelled), return_when=asyncio.FIRST_COMPLETED)
            token.raise_if_cancelled()
            if not model.done() or clock.now() >= limit:
                child.cancel("deadline")
                return None
            try:
                turn = model.result()
            except (Exception, asyncio.CancelledError):
                return None
            observed = True
            agent.protection.update(turn.quota)
            result, fields = None, {"status": "failed"}
            if turn.finish == "stop" and not turn.tool_calls:
                try:
                    result, validated = parse(turn.text or "")
                    fields = {"status": "ok", **validated}
                except ValueError:
                    pass
            await usage(turn, "on_time", fields)
            token.raise_if_cancelled()
            return result
        finally:
            timer.cancel()
            cancelled.cancel()
            try:
                if not observed:
                    child.cancel("cancelled")
                    finalization.track(asyncio.create_task(late()))
                    # No token estimate for a call without an on-time turn.
                    await audit("attention.failed", status="failed")
                await asyncio.gather(timer, cancelled, return_exceptions=True)
            finally:
                if own:
                    finalization.release()

    def read_status(self):
        today = (
            self.agent.clock.now()
            .astimezone(ZoneInfo(self.agent.instance.get("timezone", "UTC")))
            .date()
            .isoformat()
        )
        rows = self.agent.ledger.db.execute(
            """SELECT json_extract(data,'$.conversation_id'),
                      sum(json_extract(data,'$.timing') IS NOT 'late'),
                      sum(coalesce(json_extract(data,'$.input_tokens'),0)
                        + coalesce(json_extract(data,'$.output_tokens'),0))
                 FROM audit WHERE json_extract(data,'$.purpose')='attention'
                   AND json_extract(data,'$.day')=?
                   AND json_extract(data,'$.kind') IN ('model.usage','attention.failed')
                 GROUP BY json_extract(data,'$.conversation_id')""",
            (today,),
        )
        result = {}
        for cid, count, tokens in rows:
            label = cid if cid in self.agent.admission.conversations else UNREGISTERED_USAGE
            totals = result.setdefault(label, {"count": 0, "tokens": 0})
            totals["count"] += count
            totals["tokens"] += tokens
        return result

    async def status(self):
        return await run_sync(self.agent.ledger, self.read_status)
