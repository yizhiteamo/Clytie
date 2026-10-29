"""对话本体的过滤、边界持久化与额度状态；时间仅来自注入时钟。"""

import json
import re
import unicodedata
from datetime import datetime, timedelta

from clytie.plugin_host.snapshots import atomic_write, canonical
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
    # 删除结构化的 @自己；其他提及或媒体不可能是纯文字取消。
    if any(
        p.type not in {"text", "mention"} or p.type == "mention" and not p.is_self
        for p in message.parts
    ):
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


class Boundary:
    def __init__(self, path, config, clock, ledger, log, notifier=None):
        self.path, self.config, self.clock, self.ledger, self.log = path, config, clock, ledger, log
        self.notifier = notifier
        self.warnings = []
        self.state = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {"actors": {}, "records": []}
        )
        self.warnings = self.state.setdefault("warnings", [])

    def save(self):
        atomic_write(self.path, canonical(self.state))

    def audit(self, kind, **data):
        with self.ledger.transaction():
            self.ledger.audit(kind, **data)

    def filtered(self, event):
        if not event.message or event.message.sender.role == "owner":
            return False
        rules = self.config.get("input_filter", {})
        value = normalized("".join(p.text for p in event.message.parts if p.type == "text"))
        return any(normalized(w) in value for w in rules.get("words", [])) or any(
            re.search(normalized(p), value) for p in rules.get("patterns", [])
        )

    def paused(self, actor):
        if actor.role in {"owner", "peer"}:
            return False
        until = self.state["actors"].get(actor.actor_id, {}).get("until")
        return bool(until and datetime.fromisoformat(until) > self.clock.now())

    async def report(self, trace, actor, category, cancel):
        enabled = self.config.get("categories", {})
        if (
            actor.role in {"owner", "peer"}
            or category not in CATEGORIES
            or not enabled.get(category, True)
        ):
            raise ValueError("invalid boundary category or actor")
        self.audit("boundary.report", trace_id=trace, actor_id=actor.actor_id, category=category)
        if trace in self.state["records"]:
            return "已记录"
        self.state["records"].append(trace)
        now = self.clock.now()
        row = self.state["actors"].setdefault(actor.actor_id, {"counts": {}})
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
                "boundary.paused", trace_id=trace, actor_id=actor.actor_id, until=row["until"]
            )
        self.save()
        if notify:
            if self.notifier is None:
                self.warnings.append("boundary_notifier_unavailable")
                self.save()
                self.log.warning("boundary.notifier_unavailable")
            else:
                await self.notifier.notify(
                    Notice(
                        level="warn",
                        title="边界暂停提醒",
                        body=f"{actor.actor_id}（昵称：{actor.display_name or ''}） {category} {row['until']}",
                        dedupe_key=f"boundary:{actor.actor_id}:{now.isoformat()}",
                    ),
                    cancel,
                )
        return "已记录"

    def release(self, companion_id, actor_id):
        if companion_id != self.ledger.companion_id:
            raise ValueError("wrong companion")
        self.state["actors"].pop(actor_id, None)
        self.save()
        self.audit("boundary.released", actor_id=actor_id)


class Outbound:
    def __init__(self, config):
        self.config = config

    def check(self, text, scene):
        value = normalized(text, compact=True)
        names = self.config.get("names", {})
        result = {}
        if scene not in names.get("internal_scenes", ["owner_private"]):
            words = self.config.get(
                "forbidden_names", [names.get("internal", "")]
            ) + self.config.get("boundary", {}).get("never_publish", [])
            hits = [
                w
                for w in words
                if normalized(w, compact=True) and normalized(w, compact=True) in value
            ]
            if hits:
                result["privacy"] = hits
        hits = [
            w
            for w in (*STYLE_WORDS, *self.config.get("outbound", {}).get("style_words", []))
            if normalized(w, compact=True) in value
        ]
        if hits:
            result["style"] = hits
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
