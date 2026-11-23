"""群聊回复安全带。收据与暂停在同一账本事务提交，提醒在提交后执行。"""

import asyncio
import json
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from clytie.backup.coordination import run_sync
from clytie.config import safety_belt_settings
from clytie.kernel.delivery import OwnedFinalization
from clytie.plugin_host.support import timed
from clytie.sdk import CancelToken
from clytie.sdk.types import Conversation, Notice, TextPart

# error_written_at 是历史账本在确认 intent 最终收据时写入的本体时间。
# 先从窗口内有收据的记录向上找到入站来源，再向下核对首次收据；
# 这样跨窗口的拆分和后来完成的子任务都只归到最早的那次回复。
REPLIES_IN_WINDOW = """
WITH RECURSIVE recent(trace) AS (
    SELECT DISTINCT json_extract(data,'$.trace_id') FROM intent
    WHERE json_extract(data,'$.kind')='reply' AND json_extract(data,'$.status')='sent'
      AND json_extract(data,'$.conversation_id')=:group
      AND json_extract(data,'$.error_written_at')>:cutoff
      AND json_extract(data,'$.error_written_at')>=:resume
), ancestors(id, source, parent) AS (
    SELECT r.id, json_extract(r.data,'$.source_event_id'), json_extract(r.data,'$.parent_trace_id')
    FROM recent JOIN record r ON r.id=recent.trace
    UNION
    SELECT r.id, json_extract(r.data,'$.source_event_id'), json_extract(r.data,'$.parent_trace_id')
    FROM ancestors a JOIN record r ON r.id=a.parent WHERE a.source IS NULL
), family(root,id) AS (
    SELECT a.id, a.id FROM ancestors a JOIN inbound e ON e.id=a.source
    WHERE json_extract(e.data,'$.conversation_id')=:group
      AND json_extract(e.data,'$.conversation.kind')='group'
      AND json_type(e.data,'$.message')='object'
    UNION
    SELECT f.root, r.id FROM family f CROSS JOIN record r INDEXED BY child_trace
    ON json_extract(r.data,'$.parent_trace_id')=f.id
    WHERE json_extract(r.data,'$.source_event_id') IS NULL
), replies AS (
    SELECT f.root, COUNT(*)=1 AND MAX(i.id)=:current AS first_confirmation
    FROM family f CROSS JOIN intent i INDEXED BY intent_trace ON json_extract(i.data,'$.trace_id')=f.id
    WHERE json_extract(i.data,'$.conversation_id')=:group
      AND json_extract(i.data,'$.kind')='reply' AND json_extract(i.data,'$.status')='sent'
    GROUP BY f.root HAVING MIN(json_extract(i.data,'$.error_written_at'))>:cutoff
                      AND MIN(json_extract(i.data,'$.error_written_at'))>=:resume
)
SELECT COUNT(*), MAX(first_confirmation) FROM replies
"""


UNREGISTERED_GROUP = "一个未登记的群"
UNREGISTERED_USAGE = "未登记的群"


def recovery_time(until, now, zone):
    target = datetime.fromisoformat(until).astimezone(zone)
    same_day = target.date() == now.astimezone(zone).date()
    return target.strftime("%H:%M" if same_day else "%Y-%m-%d %H:%M")


def notice_body(group, count, until):
    return f"Clytie 系统通知：{group} 今天第 {count} 次触发群聊安全带，恢复时间 {until}。"


class SafetyBelt:
    def __init__(self, agent, *, notifier=None):
        self.agent = agent
        self.ledger = agent.ledger
        self.settings = safety_belt_settings(agent.config)
        self.zone = ZoneInfo(agent.instance.get("timezone", "UTC"))
        self.notifier = notifier
        self.notifications = set()
        agent.kernel.delivery.safety_belt = self

    def _latest(self, group):
        if group in self.agent.admission.conversations:
            row = self.ledger.db.execute(
                "SELECT data FROM audit WHERE json_extract(data,'$.kind')='safety_belt.tripped' "
                "AND json_extract(data,'$.conversation_id')=? ORDER BY id DESC LIMIT 1",
                (group,),
            ).fetchone()
        else:
            # 用既有回复记录关联真实会话，不在安全带审计或对外投影中保存匿名编号。
            row = self.ledger.db.execute(
                "SELECT a.data FROM audit a JOIN record r ON r.id=json_extract(a.data,'$.trace_id') "
                "WHERE json_extract(a.data,'$.kind')='safety_belt.tripped' "
                "AND json_extract(a.data,'$.conversation_id')=? "
                "AND json_extract(r.data,'$.conversation_id')=? ORDER BY a.id DESC LIMIT 1",
                (UNREGISTERED_GROUP, group),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def paused(self, group):
        trip = self._latest(group)
        return bool(trip and self.agent.clock.now() < datetime.fromisoformat(trip["until"]))

    def _day(self, timestamp):
        return datetime.fromisoformat(timestamp).astimezone(self.zone).date()

    def _owner_target(self):
        admission = self.agent.admission
        return next(
            (
                key
                for key, actor in admission.private_actors.items()
                if actor.role == "owner"
                and admission.conversations[key]["scene"] == "owner_private"
                and any(
                    c["plugin"] == admission.conversations[key]["channel"]
                    for c in self.agent.config["channels"]
                )
            ),
            None,
        )

    def read_status(self):
        rows = self.ledger.db.execute(
            "SELECT a.data FROM audit a WHERE a.id IN (SELECT MAX(t.id) FROM audit t "
            "LEFT JOIN record r ON r.id=json_extract(t.data,'$.trace_id') "
            "WHERE json_extract(t.data,'$.kind')='safety_belt.tripped' "
            "GROUP BY coalesce(json_extract(r.data,'$.conversation_id'), "
            "json_extract(t.data,'$.conversation_id')))"
        )
        now = self.agent.clock.now()
        today = now.astimezone(self.zone).date()
        missing = self._owner_target() is None
        result = {}
        for (raw,) in rows:
            trip = json.loads(raw)
            count = trip["count"] if self._day(trip["at"]) == today else 0
            if not count and datetime.fromisoformat(trip["until"]) <= now:
                continue
            row = dict(count=count, until=trip["until"], owner_private_missing=missing)
            group = trip["conversation_id"]
            if group in self.agent.admission.conversations:
                result[group] = row
            else:
                result.setdefault(UNREGISTERED_GROUP, []).append(row)
        return result

    def _confirmed(self, intent):
        group = intent["conversation_id"]
        registered = self.agent.admission.conversations.get(group, {})
        if intent["kind"] != "reply":
            return None
        previous = self._latest(group)
        now = self.agent.clock.now()
        if previous and now < datetime.fromisoformat(previous["until"]):
            return None
        window = self.settings["window_minutes"]
        cutoff = (now - timedelta(minutes=window)).isoformat()
        resume = previous["until"] if previous else ""
        threshold = self.settings["replies"]
        # 通常连发送的条数都达不到阈值。只读窗口索引，达到阈值才核对入站来源。
        enough_intents = self.ledger.db.execute(
            "SELECT 1 FROM intent WHERE json_extract(data,'$.kind')='reply' "
            "AND json_extract(data,'$.status')='sent' AND json_extract(data,'$.conversation_id')=? "
            "AND json_extract(data,'$.error_written_at')>? AND json_extract(data,'$.error_written_at')>=? LIMIT 1 OFFSET ?",
            (group, cutoff, resume, threshold - 1),
        ).fetchone()
        if not enough_intents:
            return None
        count, first = self.ledger.db.execute(
            REPLIES_IN_WINDOW,
            {"group": group, "current": intent["intent_id"], "cutoff": cutoff, "resume": resume},
        ).fetchone()
        if count < threshold or not first:
            return None
        daily = 1
        if previous and self._day(previous["at"]) == now.astimezone(self.zone).date():
            daily += previous["count"]
        trip = dict(
            conversation_id=group if registered.get("kind") == "group" else UNREGISTERED_GROUP,
            count=daily,
            window_minutes=window,
            until=(now + timedelta(minutes=self.settings["pause_minutes"])).isoformat(),
        )
        self.ledger.audit("safety_belt.tripped", trace_id=intent["trace_id"], **trip)
        return trip

    def record_receipt(self, intent_id, n, receipt, *, retry):
        trip = None

        def confirmed(intent):
            nonlocal trip
            trip = self._confirmed(intent)

        fresh = self.ledger.record_receipt(
            intent_id, n, receipt, retry=retry, on_confirmed=confirmed
        )
        return fresh, trip

    def notify(self, trip):
        display = {**trip, "until": recovery_time(trip["until"], self.agent.clock.now(), self.zone)}
        self.agent.log.warning("safety_belt.tripped", **display)
        notice = Notice(
            level="warn",
            title="Clytie",
            body=notice_body(display["conversation_id"], display["count"], display["until"]),
            dedupe_key=f"safety_belt:{self.agent.cid}:{uuid4().hex}",
        )
        self._track(self._notify_channel(trip, notice))
        target = self._owner_target()
        if target:
            self._track(self._notify_owner(target, display, notice.body))

    def _track(self, operation):
        task = self.agent.track(operation)
        self.notifications.add(task)
        task.add_done_callback(self.notifications.discard)

    async def drain(self):
        while self.notifications:
            await asyncio.gather(*tuple(self.notifications), return_exceptions=True)

    async def _notify_channel(self, trip, notice):
        if self.notifier is not None:
            token = CancelToken()
            try:
                result = await timed(self.notifier.notify(notice, token), self.agent.clock, 10)
                if result.status == "sent":
                    return
            except Exception:
                pass
            finally:
                token.cancel("notification_finished")
        elif getattr(self.agent, "forward_safety_trip", None):
            self.agent.forward_safety_trip(trip)
            return
        self.agent.log.warning("safety_belt.notifier_unavailable")

    async def _notify_owner(self, target, trip, body):
        agent = self.agent
        try:
            submission = await agent.submit(
                target,
                topic="告诉主人，你在这个群里好像和别的机器人聊得停不下来了，想先歇一会儿，也说说什么时候回来",
                material={key: trip[key] for key in ("conversation_id", "count", "until")},
                reason="safety_belt",
            )
            trace = submission.trace_id
            future = agent.kernel.queued.get(trace)
            if future is not None:
                await asyncio.shield(future)
            record = await run_sync(self.ledger, self.ledger.get, "record", trace)
            if record and (record["delivery_status"] != "none" or record["cancelled"]):
                return
            if agent.shutting_down or agent.kernel.delivery.silenced:
                return
            await self._system_notice(target, body)
        except Exception:
            agent.log.warning("safety_belt.owner_notification_failed")

    async def _system_notice(self, target, body):
        agent = self.agent
        cfg = agent.admission.conversations[target]
        channel = next(
            c["account"] for c in agent.config["channels"] if c["plugin"] == cfg["channel"]
        )
        conversation = Conversation(
            conversation_id=target, kind="private", scene=cfg["scene"], channel_id=channel
        )
        if await agent.outbound.check(body, conversation.scene):
            agent.log.warning("safety_belt.system_notification_blocked")
            return
        delivery = agent.kernel.delivery
        finalization = OwnedFinalization(delivery)
        try:
            record = await agent.kernel.open_record(
                conversation=conversation, finalization=finalization
            )
            trace = record["trace_id"]
            agent._bind_finalization(trace, finalization)
            intent = agent.intent(record, [TextPart(text=body)], kind="proactive")
            await agent.finish(trace, "ok", [intent])
        finally:
            finalization.release()
