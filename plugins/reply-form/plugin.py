"""聊天回复的分段、引用和交付间隔。"""

import re
from datetime import timedelta

from clytie.sdk.types import ContextSection, TextPart


def split_text(text, limit):
    """在原字符串上选断点，连同空行、标点和空白原样保留。"""
    start = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    cuts = [
        m.end() for m in re.finditer(r"\r?\n[ \t]*(?:\r?\n[ \t]*)+", text) if start < m.end() < end
    ]
    if cuts:
        cuts = cuts[: limit - 1]
    else:
        ends = [m.end() for m in re.finditer(r"[。！？!?…]+", text) if m.end() < end]
        groups = min(limit, len(ends) + 1)
        cuts = []
        for i in range(1, groups):
            # 给其余段保留足够的断点。
            available = ends[: len(ends) - (groups - i - 1)]
            cut = min(available, key=lambda p: abs(p - len(text) * i / groups))
            cuts.append(cut)
            ends = [p for p in ends if p > cut]
    return [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)])]


class Plugin:
    async def start(self, ctx):
        self.ctx = ctx
        self.tuning = ctx.config.get("tuning", {})
        self.interval = self.tuning.get("interval_seconds", [1, 2])
        if self.interval[0] > self.interval[1]:
            raise ValueError("invalid interval_seconds")

    async def stop(self):
        self.ctx = None

    async def shape(self, draft):
        group = draft.conversation.kind == "group"
        if group and draft.focus and draft.interleaved:
            draft = draft.model_copy(update={"quote": draft.focus})
        if draft.task_id or not all(isinstance(p, TextPart) for p in draft.parts):
            return draft
        text = "".join(p.text for p in draft.parts)
        if len("".join(text.split())) <= self.tuning.get("min_chars", 60):
            return draft
        limit = (
            self.tuning.get("max_parts_group", 2)
            if group
            else self.tuning.get("max_parts_private", 3)
        )
        chunks = split_text(text, limit)
        if len(chunks) < 2:
            return draft
        return draft.model_copy(
            update={"form": "split", "parts": [TextPart(text=c) for c in chunks]}
        )

    async def pace(self, intents):
        previous = {}
        output = []
        for intent in intents:
            if intent.kind == "reply" and intent.split and not intent.task_id:
                group = intent.split.group_id
                when = intent.not_before or self.ctx.clock.now()
                if group in previous:
                    interval = self.ctx.random.uniform(*self.interval)
                    when = max(when, previous[group] + timedelta(seconds=interval))
                    intent = intent.model_copy(update={"not_before": when})
                previous[group] = when
            output.append(intent)
        return output

    async def hint(self, request):
        if request.conversation is None:
            return None
        return ContextSection(
            title="聊天习惯",
            text="聊天时可以像平常发消息那样，用空行分成几句说，不必每次都分。",
            priority=100,
        )
