"""行为调用协议及无副作用的合并、形式校验函数。"""

from collections.abc import Sequence
from typing import Annotated, Literal, Protocol

from pydantic import Field

from .types import (
    CardPart,
    CompanionState,
    ContextSection,
    DeliveryIntent,
    EventEnvelope,
    JsonObject,
    LinkPart,
    Message,
    MessagePart,
    MessageRef,
    ReplyDraft,
    SDKModel,
    TaskSummary,
    TextPart,
)


class Ignore(SDKModel):
    type: Literal["ignore"] = "ignore"


class ReactOnly(SDKModel):
    type: Literal["react_only"] = "react_only"
    emoji: str


class Mode(SDKModel):
    type: Literal["mode"] = "mode"
    name: str
    params: JsonObject


class Normal(SDKModel):
    type: Literal["normal"] = "normal"


class Skip(SDKModel):
    type: Literal["skip"] = "skip"


class Defer(SDKModel):
    type: Literal["defer"] = "defer"
    seconds: float = Field(ge=0)
    prepare: bool = False


class JudgeFacts(SDKModel):
    participants: int = Field(ge=0, strict=True)
    share: float = Field(ge=0, le=1, strict=True)
    rounds_with_same_person: int = Field(ge=0, strict=True)
    counterpart_fast: bool = Field(strict=True)
    fatigue: Literal["none", "some", "high"]
    ignored_recently: bool = Field(strict=True)
    owner_spoke: bool = Field(strict=True)
    name_hit: bool = Field(strict=True)
    follow_up: Literal["none", "partner", "others"] = "none"


class Judge(SDKModel):
    type: Literal["judge"] = "judge"
    facts: JudgeFacts
    prepare: bool = False


class Judgment(SDKModel):
    status: Literal["ok", "failed"]
    join: bool = False
    focus: MessageRef | None = None
    new_topic: bool = False
    calling_me: bool | None = None


class Respond(SDKModel):
    focus: MessageRef | None = None
    recheck_after: int | None = Field(default=None, ge=1)
    type: Literal["respond"] = "respond"


BeforeAttentionDirective = Annotated[
    Ignore | ReactOnly | Mode | Normal, Field(discriminator="type")
]
AttentionDirective = Annotated[Skip | Defer | Judge | Respond, Field(discriminator="type")]
Directive = Annotated[
    Ignore | ReactOnly | Mode | Normal | Skip | Defer | Judge | Respond, Field(discriminator="type")
]


class MessageReceivedHandler(Protocol):
    async def __call__(self, input: EventEnvelope) -> None: ...


class BeforeAttentionHandler(Protocol):
    async def __call__(
        self, input: tuple[EventEnvelope, CompanionState]
    ) -> BeforeAttentionDirective | None: ...


class AttentionHandler(Protocol):
    async def __call__(
        self,
        input: tuple[EventEnvelope, list[Message]] | tuple[EventEnvelope, list[Message], Judgment],
    ) -> AttentionDirective | None: ...


class AfterGenerationHandler(Protocol):
    async def __call__(self, input: ReplyDraft) -> ReplyDraft | None: ...


class BeforeSendHandler(Protocol):
    async def __call__(self, input: list[DeliveryIntent]) -> list[DeliveryIntent] | None: ...


class TaskCompletedHandler(Protocol):
    async def __call__(self, input: TaskSummary) -> None: ...


def merge_before_attention(
    opinions: Sequence[tuple[int, str, BeforeAttentionDirective | None]],
    *,
    owner_private: bool,
    fallback_emoji: str = "💤",
) -> BeforeAttentionDirective | None:
    strengths = {Ignore: 4, ReactOnly: 3, Mode: 2, Normal: 1}
    ordered = sorted(opinions, key=lambda item: (item[0], item[1]))
    available = [directive for _, _, directive in ordered if directive is not None]
    result = max(available, key=lambda item: strengths[type(item)], default=None)
    if owner_private and isinstance(result, Ignore):
        return ReactOnly(emoji=fallback_emoji)
    return result


def merge_attention(
    opinions: Sequence[tuple[int, str, AttentionDirective | None]],
    *,
    private: bool,
    mentioned: bool,
    owner_private: bool,
    max_delay: float = 120,
) -> AttentionDirective:
    if max_delay < 0:
        raise ValueError("max_delay must be nonnegative")
    ordered = sorted(opinions, key=lambda item: (item[0], item[1]))
    available = [directive for _, _, directive in ordered if directive is not None]
    if any(isinstance(item, Skip) for item in available):
        return Respond() if owner_private else Skip()
    delays = [item.seconds for item in available if isinstance(item, Defer)]
    prepare = all(item.prepare for item in available if isinstance(item, (Judge, Defer)))
    if delays:
        return Defer(seconds=min(max(delays), max_delay), prepare=prepare)
    for item in available:
        if isinstance(item, Judge):
            return Respond() if owner_private else item.model_copy(update={"prepare": prepare})
    for item in available:
        if isinstance(item, Respond):
            return item
    if private or mentioned or owner_private:
        return Respond()
    return Skip()


def _text(parts: Sequence[MessagePart]) -> str:
    chunks = []
    for part in parts:
        if isinstance(part, TextPart):
            chunks.append(part.text)
        elif isinstance(part, CardPart):
            chunks.extend((part.title, part.body))
        elif isinstance(part, LinkPart):
            chunks.extend((part.url, part.title or ""))
    return "".join("".join(chunks).split())


def valid_after_generation(before: ReplyDraft, after: ReplyDraft) -> bool:
    """供宿主决定是否丢弃修改；此函数不执行插件或写日志。"""
    if before.model_dump(exclude={"parts", "form", "quote"}) != after.model_dump(
        exclude={"parts", "form", "quote"}
    ):
        return False
    if _text(before.parts) != _text(after.parts):
        return False

    # 资产、提及等不属于可修改的文字形式，避免用形式转换替换内容。
    def non_text(parts):
        return [part for part in parts if not isinstance(part, (TextPart, CardPart, LinkPart))]

    return non_text(before.parts) == non_text(after.parts)


def valid_before_send(before: Sequence[DeliveryIntent], after: Sequence[DeliveryIntent]) -> bool:
    if len(before) != len(after):
        return False
    allowed = {"target", "split", "not_before"}
    return all(
        original.model_dump(exclude=allowed) == changed.model_dump(exclude=allowed)
        for original, changed in zip(before, after)
    )


def collect_context_sections(
    sections: Sequence[tuple[ContextSection, int]], total_budget_chars: int
) -> list[ContextSection]:
    """先按每段预算截断，再从优先级最低的段落开始整段舍弃。"""
    if total_budget_chars < 0 or any(budget < 0 for _, budget in sections):
        raise ValueError("context budgets must be nonnegative")
    result = [
        section.model_copy(update={"text": section.text[:budget]})
        for section, budget in sorted(sections, key=lambda item: item[0].priority)
    ]
    size = sum(len(section.text) for section in result)
    while result and size > total_budget_chars:
        size -= len(result.pop().text)
    return result
