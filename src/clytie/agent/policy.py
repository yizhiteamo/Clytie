"""对话本体的过滤、边界持久化与额度状态；时间仅来自注入时钟。"""

import asyncio
import json
import threading
import unicodedata
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta
from itertools import groupby
from operator import attrgetter

from clytie.backup.coordination import run_sync
from clytie.config import compile_input_pattern
from clytie.kernel.events import background
from clytie.plugin_host.snapshots import atomic_write, canonical
from clytie.plugin_host.support import timed
from clytie.sdk import CancelToken
from clytie.sdk.types import Notice

STYLE_WORDS = ("作为一个 AI 语言模型", "工具调用", "函数调用", "系统提示", "tool_call")
CATEGORIES = ("romance", "persona_change", "scripted_intimacy", "intimate_images", "owner_privacy")


def text_parts(parts):
    return "\n".join(
        p.text
        if p.type == "text"
        else p.title + "\n" + p.body
        if p.type == "card"
        else (p.title or "")
        if p.type == "link"
        else ""
        for p in parts
    )


def normalized(value, *, compact=False):
    value = unicodedata.normalize("NFKC", value).lower()
    return (
        "".join(c for c in value if not c.isspace() and unicodedata.category(c) != "Cf")
        if compact
        else value
    )


def fast_cancel(message, words):
    seen_quote = False
    for part in message.parts:
        if part.type == "text" or (part.type == "mention" and part.is_self):
            continue
        if part.type == "quote" and not seen_quote and part.message_ref == message.reply_to:
            seen_quote = True
            continue
        return None
    value = "".join(p.text for p in message.parts if p.type == "text")

    def edge(c):
        return c.isspace() or unicodedata.category(c)[0] in {"P", "S"} or c in {"\ufe0f", "\u200d"}

    while value and edge(value[0]):
        value = value[1:]
    while value and edge(value[-1]):
        value = value[:-1]
    for word in words:
        if value == word:
            return {}
        if value.startswith(word):
            suffix = value[len(word) :].strip().lower()
            if len(suffix) == 8 and all(c in "0123456789abcdef" for c in suffix):
                return {"task_id": suffix}
    return None


def actor_label(actor):
    return f"{actor.actor_id}（昵称：{actor.display_name or ''}）"


class Boundary:
    def __init__(self, path, config, clock, ledger, log, notifier=None):
        self.path, self.config, self.clock, self.ledger, self.log = path, config, clock, ledger, log
        self.notifier = notifier
        self.notifications = set()
        self.notification_timeout = 2
        self.patterns = [
            compile_input_pattern(p) for p in config.get("input_filter", {}).get("patterns", [])
        ]
        self._state = None
        self._view = {"actors": {}, "records": [], "warnings": []}
        self._loop = None
        self._loop_thread = None
        from clytie.storage.handle import StoreHandle

        if not isinstance(ledger, StoreHandle):
            self._load()

    @property
    def state(self):
        return deepcopy(self._view)

    @property
    def warnings(self):
        return list(self._view["warnings"])

    def _publish(self):
        snapshot = deepcopy(self._state)
        if self._loop is None or threading.get_ident() == self._loop_thread:
            self._view = snapshot
        else:
            self._loop.call_soon_threadsafe(self._set_view, snapshot)

    def _set_view(self, snapshot):
        self._view = snapshot

    def _load(self):
        if self._state is None:
            self._state = (
                json.loads(self.path.read_text(encoding="utf-8"))
                if self.path.exists()
                else {"actors": {}, "records": []}
            )
            self._state.setdefault("warnings", [])
        self._publish()

    async def initialize(self):
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        await run_sync(self.ledger, self._load)

    def save(self):
        coordinator = self.ledger.coordination
        with coordinator.commit() if coordinator else nullcontext():
            atomic_write(self.path, canonical(self._state))
        self._publish()

    def _add_warning(self, warning):
        self._load()
        if warning not in self._state["warnings"]:
            self._state["warnings"].append(warning)
            self.save()

    async def add_warning(self, warning):
        await run_sync(self.ledger, self._add_warning, warning)

    def audit(self, kind, **data):
        with self.ledger.transaction():
            self.ledger.audit(kind, **data)

    def filtered(self, event):
        if not event.message or event.message.sender.role == "owner":
            return False
        rules = self.config.get("input_filter", {})
        value = normalized("".join(p.text for p in event.message.parts if p.type == "text"))
        return any(normalized(w) in value for w in rules.get("words", [])) or any(
            p.search(value) for p in self.patterns
        )

    def paused(self, actor):
        if actor.role in {"owner", "peer"}:
            return False
        until = self._view["actors"].get(actor.actor_id, {}).get("until")
        return bool(until and datetime.fromisoformat(until) > self.clock.now())

    def reminder(self, actors):
        cutoff = self.clock.now() - timedelta(hours=self.config.get("window_hours", 24))
        recent = []
        for actor in {a.actor_id: a for a in actors}.values():
            if actor.role in {"owner", "peer"} or self.paused(actor):
                continue
            counts = self._view["actors"].get(actor.actor_id, {}).get("counts", {})
            times = [
                datetime.fromisoformat(t)
                for values in counts.values()
                for t in values
                if datetime.fromisoformat(t) > cutoff
            ]
            if times:
                recent.append((max(times), actor))
        recent.sort(key=lambda item: item[1].actor_id)
        recent.sort(key=lambda item: item[0], reverse=True)
        return (
            "近期试过越界请求的人：" + "、".join(actor_label(a) for _, a in recent[:10])
            if recent
            else None
        )

    async def report(self, trace, actor, category, cancel):
        # 同一次报告的账本证据与持久计数不能被捕获边界切开。
        # 异步等待门闩后只做同步提交，通知安排在后台执行。
        await self.initialize()
        notice = await run_sync(self.ledger, self._report, trace, actor, category, cancel)
        if notice is not None:
            task = background(self.notify(notice))
            self.notifications.add(task)
            task.add_done_callback(self.notifications.discard)
        return "已记录"

    def _report(self, trace, actor, category, cancel):
        self._load()
        enabled = self.config.get("categories", {})
        if (
            actor.role in {"owner", "peer"}
            or category not in CATEGORIES
            or not enabled.get(category, True)
        ):
            raise ValueError("invalid boundary category or actor")
        self.audit(
            "boundary.report",
            trace_id=trace,
            actor_id=actor.actor_id,
            category=category,
        )
        if trace in self._state["records"]:
            return None
        self._state["records"].append(trace)
        now = self.clock.now()
        row = self._state["actors"].setdefault(actor.actor_id, {"counts": {}})
        cutoff = now - timedelta(hours=self.config.get("window_hours", 24))
        counts = [t for t in row["counts"].get(category, []) if datetime.fromisoformat(t) > cutoff]
        counts.append(now.isoformat())
        row["counts"][category] = counts
        notify = False
        if len(counts) >= self.config.get("refusal_threshold", 5):
            row["until"] = (now + timedelta(hours=self.config.get("pause_hours", 24))).isoformat()
            previous = row.get("notified_at")
            notify = self.config.get("notify_owner", True) and (
                not previous or now - datetime.fromisoformat(previous) >= timedelta(hours=24)
            )
            if notify:
                row["notified_at"] = now.isoformat()
            self.audit(
                "boundary.paused",
                trace_id=trace,
                actor_id=actor.actor_id,
                until=row["until"],
            )
        self.save()
        if notify:
            if self.notifier is None:
                self._add_warning("boundary_notifier_unavailable")
                self.log.warning("boundary.notifier_unavailable")
            else:
                return Notice(
                    level="warn",
                    title="边界暂停提醒",
                    body=f"{actor.actor_id}（昵称：{actor.display_name or ''}） {category} {row['until']}",
                    dedupe_key=f"boundary:{actor.actor_id}:{now.isoformat()}",
                )
        return None

    async def notify(self, notice):
        token = CancelToken()
        try:
            await timed(self.notifier.notify(notice, token), self.clock, self.notification_timeout)
        except (Exception, asyncio.CancelledError):
            await self.add_warning("boundary_notification_failed")
            self.log.warning("boundary.notification_failed")
        finally:
            token.cancel("notification_finished")

    async def drain_notifications(self):
        while self.notifications:
            await asyncio.gather(*tuple(self.notifications))

    async def release(self, companion_id, actor_id):
        await self.initialize()
        await run_sync(self.ledger, self._release, companion_id, actor_id)

    def _release(self, companion_id, actor_id):
        if companion_id != self.ledger.companion_id:
            raise ValueError("wrong companion")
        self._load()
        row = self._state["actors"].get(actor_id)
        if row is not None:
            row["counts"] = {}
            row.pop("until", None)
        self.save()
        self.audit("boundary.released", actor_id=actor_id)


class Outbound:
    def __init__(self, config):
        self.config = config
        self.memory_guard = None

    @property
    def forbidden_names(self):
        return self.config.get(
            "forbidden_names", [self.config.get("names", {}).get("internal", "")]
        )

    @staticmethod
    def _matches(words, value):
        for word in words:
            needle = normalized(word, compact=True)
            if needle and needle in value:
                yield word

    def names_in_batch(self, events):
        """只匹配每条消息中连续的文字段，链接、引用等不能拼出名字。"""
        names = set()
        for event in events:
            for kind, parts in groupby(event.message.parts, key=attrgetter("type")):
                if kind == "text":
                    value = normalized("".join(part.text for part in parts), compact=True)
                    names.update(self._matches(self.forbidden_names, value))
        return names

    async def check(self, text, scene, *, spoken_names=()):
        value = normalized(text, compact=True)
        names = self.config.get("names", {})
        result = {}
        if scene not in names.get("internal_scenes", ["owner_private"]):
            hits = [w for w in self._matches(self.forbidden_names, value) if w not in spoken_names]
            never_publish = self.config.get("boundary", {}).get("never_publish", [])
            hits.extend(self._matches(never_publish, value))
            if hits:
                result["privacy"] = hits
        hits = [
            w
            for w in (*STYLE_WORDS, *self.config.get("outbound", {}).get("style_words", []))
            if normalized(w, compact=True) in value
        ]
        if hits:
            result["style"] = hits
        if self.memory_guard and await self.memory_guard(text, scene):
            result.setdefault("privacy", []).append("包含了不能在这里说的私密内容")
        return result


class Protection:
    def __init__(self, config, emit, model_plugin):
        self.config, self.emit, self.model_plugin = config, emit, model_plugin
        self.active, self.snapshot = False, None

    def update(self, snapshot):
        if snapshot is None or snapshot == "unknown":
            return
        self.snapshot = snapshot
        ratios = [w.remaining_ratio for w in snapshot.windows]
        active = self.active
        if ratios and any(r <= self.config.get("enter_ratio", 0.2) for r in ratios):
            active = True
        elif ratios and all(r > self.config.get("exit_ratio", 0.3) for r in ratios):
            active = False
        if active != self.active:
            self.active = active
            self.emit(
                "quota.changed",
                dict(
                    model_plugin=self.model_plugin,
                    snapshot=snapshot.model_dump(mode="json"),
                    protection_mode=active,
                ),
            )
