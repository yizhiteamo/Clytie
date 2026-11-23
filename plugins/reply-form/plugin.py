"""聊天回复的分段、引用和交付间隔。"""

import re
from datetime import timedelta

from clytie.sdk.types import ContextSection, TextPart

# HTTP URLs can contain ASCII sentence punctuation in the path, query and fragment.
URL = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", re.IGNORECASE)
SENTENCE = re.compile(r"""([。！？!?…]+)[”’」』）)】》〉〕\]"']*""")


def sentence_ends(text):
    urls = iter(URL.finditer(text))
    url = next(urls, None)
    for mark in SENTENCE.finditer(text):
        while url and url.end() <= mark.start():
            url = next(urls, None)
        if url and url.start() <= mark.start() and mark.end(1) < url.end():
            continue
        following = text[mark.end(1) : mark.end(1) + 1]
        if mark[1][-1] in "?!" and following.isascii() and following.isalnum():
            continue
        if mark.end() < len(text):
            yield mark.end()


def split_text(text, limit):
    """优先在空行处分条，然后选择靠近等分点的句末。"""
    text = text.strip()
    cuts = [
        m.end() for m in re.finditer(r"\r?\n[ \t]*(?:\r?\n[ \t]*)+", text) if m.end() < len(text)
    ]
    if cuts:
        cuts = cuts[: limit - 1]
    else:
        ends = list(sentence_ends(text))
        groups = min(limit, len(ends) + 1)
        cuts = []
        for i in range(1, groups):
            # 给其余段保留足够的断点。
            available = ends[: len(ends) - (groups - i - 1)]
            cut = min(available, key=lambda p: abs(p - len(text) * i / groups))
            cuts.append(cut)
            ends = [p for p in ends if p > cut]
    return [text[a:b].strip() for a, b in zip([0, *cuts], [*cuts, len(text)])]


class Plugin:
    async def start(self, ctx):
        self.ctx = ctx
        self.tuning = ctx.config.get("tuning", {})
        self.interval = self.tuning.get("interval_seconds", [1, 2])
        if self.interval[0] > self.interval[1]:
            raise ValueError("invalid interval_seconds")
        self.proactive_interval = self.tuning.get("interval_seconds_proactive", [2, 4])
        if self.proactive_interval[0] > self.proactive_interval[1]:
            raise ValueError("invalid interval_seconds_proactive")

    async def stop(self):
        self.ctx = None

    async def shape(self, draft):
        group = draft.conversation.kind == "group"
        proactive = getattr(draft, "kind", "reply") == "proactive"
        if group and draft.focus and draft.interleaved:
            draft = draft.model_copy(update={"quote": draft.focus})
        if (draft.task_id and not proactive) or not all(
            isinstance(p, TextPart) for p in draft.parts
        ):
            return draft
        text = "".join(p.text for p in draft.parts)
        if len("".join(text.split())) <= self.tuning.get("min_chars", 60):
            return draft
        if proactive:
            limit = self.tuning.get("max_parts_proactive", 6)
        elif group:
            limit = self.tuning.get("max_parts_group", 2)
        else:
            limit = self.tuning.get("max_parts_private", 3)
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
            if intent.split and (
                intent.kind == "proactive" or (intent.kind == "reply" and not intent.task_id)
            ):
                group = intent.split.group_id
                when = intent.not_before or self.ctx.clock.now()
                if group in previous:
                    bounds = (
                        self.proactive_interval if intent.kind == "proactive" else self.interval
                    )
                    interval = self.ctx.random.uniform(*bounds)
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
