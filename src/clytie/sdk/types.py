"""SDK 0.2 的不可变、可序列化数据契约。"""

from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    StringConstraints,
    model_validator,
)

from .immutable import FrozenDict, FrozenList, freeze, thaw


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(timezone.utc)


UTCDateTime = Annotated[datetime, AfterValidator(_utc)]
TraceId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
TaskId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8}$")]
AssetId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
JsonObject = FrozenDict[JsonValue]
FrozenJson = Annotated[JsonValue, AfterValidator(freeze), PlainSerializer(thaw)]


def _decode_bytes(value, info):
    if info.mode == "json":
        if not isinstance(value, str):
            raise ValueError("JSON bytes must be a standard Base64 string")
        return base64.b64decode(value, validate=True)
    return value


JsonBytes = Annotated[
    bytes,
    BeforeValidator(_decode_bytes),
    PlainSerializer(
        lambda value: base64.b64encode(value).decode("ascii"), return_type=str, when_used="json"
    ),
]
QUOTA_UNKNOWN: Literal["unknown"] = "unknown"


class SDKModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        allow_inf_nan=False,
    )

    def model_copy(self, *, update=None, deep=False):
        """拷贝仍经过校验，防止 Pydantic 的未校验更新引入可变字段。"""
        values = self.model_dump(mode="python")
        values.update(update or {})
        return type(self).model_validate(values)

    @classmethod
    def model_construct(cls, _fields_set=None, **values):
        """SDK 不提供跳过校验的构造入口。"""
        return cls.model_validate(values)


class Visibility(str, Enum):
    PRIVATE = "private"
    PUBLIC = "public"
    RELATIONSHIP = "relationship"


class Actor(SDKModel):
    actor_id: str
    role: Literal["owner", "peer", "member", "stranger"]
    display_name: str | None = None
    channel_id: str


class Conversation(SDKModel):
    conversation_id: str
    kind: Literal["private", "group"]
    scene: str
    channel_id: str


class MessageRef(SDKModel):
    channel_id: str
    conversation_id: str
    platform_message_id: str


class AssetRef(SDKModel):
    asset_id: AssetId
    media_type: str
    size: int = Field(ge=0)
    origin: str


class TextPart(SDKModel):
    type: Literal["text"] = "text"
    text: str


class MentionPart(SDKModel):
    type: Literal["mention"] = "mention"
    actor_id: str
    is_self: bool


class MediaPart(SDKModel):
    asset: AssetRef | None = None
    raw_ref: str | None = None


class ImagePart(MediaPart):
    type: Literal["image"] = "image"


class AudioPart(MediaPart):
    type: Literal["audio"] = "audio"


class VideoPart(MediaPart):
    type: Literal["video"] = "video"


class FilePart(MediaPart):
    type: Literal["file"] = "file"


class LinkPart(SDKModel):
    type: Literal["link"] = "link"
    url: str
    title: str | None = None


class QuotePart(SDKModel):
    type: Literal["quote"] = "quote"
    message_ref: MessageRef


class StickerPart(SDKModel):
    type: Literal["sticker"] = "sticker"
    sticker_id: str
    asset: AssetRef | None = None


class CardPart(SDKModel):
    type: Literal["card"] = "card"
    title: str
    body: str


class UnknownPart(SDKModel):
    type: Literal["unknown"] = "unknown"
    raw_type: str


MessagePart = Annotated[
    TextPart
    | MentionPart
    | ImagePart
    | AudioPart
    | VideoPart
    | FilePart
    | LinkPart
    | QuotePart
    | StickerPart
    | CardPart
    | UnknownPart,
    Field(discriminator="type"),
]


class Message(SDKModel):
    ref: MessageRef
    sender: Actor
    parts: FrozenList[MessagePart] = Field(default_factory=FrozenList)
    sent_at: UTCDateTime
    reply_to: MessageRef | None = None


class ConversationNotice(SDKModel):
    kind: Literal["recall", "member_join", "member_leave", "poke", "other"]
    actor_id: str | None = None
    target: MessageRef | None = None
    detail: str | None = None


class ConversationRequest(SDKModel):
    kind: Literal["friend", "group_invite"]
    actor_id: str
    comment: str | None = None
    request_ref: str


class EventEnvelope(SDKModel):
    event_id: str
    type: str
    companion_id: str
    channel_id: str
    conversation: Conversation
    message: Message | None = None
    notice: ConversationNotice | ConversationRequest | None = None
    received_at: UTCDateTime
    raw_ref: str | None = None

    @model_validator(mode="after")
    def check_message(self):
        notice_type = {
            "conversation.notice": ConversationNotice,
            "conversation.request": ConversationRequest,
        }.get(self.type)
        if notice_type is not None and not isinstance(self.notice, notice_type):
            raise ValueError(f"{self.type} requires {notice_type.__name__}")
        if not self.event_id.startswith(f"{self.channel_id}:"):
            raise ValueError("event_id must start with channel_id and a colon")
        if self.type == "conversation.message":
            if self.message is None:
                raise ValueError("conversation.message requires message")
            expected = f"{self.channel_id}:{self.message.ref.platform_message_id}"
            if self.event_id != expected:
                raise ValueError("event_id must match the platform message reference")
        return self


class DeliverySplit(SDKModel):
    group_id: str
    index: int
    total: int


class DeliveryIntent(SDKModel):
    intent_id: TraceId
    companion_id: str
    conversation_id: str
    channel_id: str
    kind: Literal["reply", "proactive", "react"]
    parts: FrozenList[MessagePart] = Field(default_factory=FrozenList)
    emoji: str | None = None
    target: MessageRef | None = None
    task_id: TaskId | None = None
    trace_id: TraceId
    split: DeliverySplit | None = None
    not_before: UTCDateTime | None = None

    @model_validator(mode="after")
    def check_reaction(self):
        if self.kind == "react" and (self.emoji is None or self.parts):
            raise ValueError("react requires emoji and empty parts")
        return self


class ErrorInfo(SDKModel):
    code: str
    message: str


class Receipt(SDKModel):
    intent_id: str
    status: Literal["sent", "failed", "unknown"]
    platform_message_id: str | None = None
    at: UTCDateTime
    error: ErrorInfo | None = None


class ToolCall(SDKModel):
    call_id: str
    tool: str
    arguments: JsonObject


class ChatMessage(SDKModel):
    role: Literal["system", "user", "assistant", "tool"]
    parts: FrozenList[MessagePart] = Field(default_factory=FrozenList)
    tool_calls: FrozenList[ToolCall] = Field(default_factory=FrozenList)
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def check_tool_fields(self):
        if self.tool_calls and self.role != "assistant":
            raise ValueError("tool_calls are only allowed for assistant messages")
        if self.tool_call_id is not None and self.role != "tool":
            raise ValueError("tool_call_id is only allowed for tool messages")
        return self


class ToolExample(SDKModel):
    input: str
    call: bool
    note: str | None = None


class ToolSpec(SDKModel):
    name: str
    description: str
    when_not_to_use: str
    examples: FrozenList[ToolExample] = Field(default_factory=FrozenList)
    parameters: JsonObject


class Budget(SDKModel):
    deadline: UTCDateTime
    max_output_tokens: int | None = None
    max_tool_rounds: int = 4


class ModelRequest(SDKModel):
    messages: FrozenList[ChatMessage] = Field(default_factory=FrozenList)
    tools: FrozenList[ToolSpec] = Field(default_factory=FrozenList)
    budget: Budget
    trace_id: TraceId


class TokenUsage(SDKModel):
    input_tokens: int | None = None
    output_tokens: int | None = None


class QuotaWindow(SDKModel):
    name: str
    remaining_ratio: float = Field(ge=0, le=1)
    resets_at: UTCDateTime | None = None


class QuotaSnapshot(SDKModel):
    windows: FrozenList[QuotaWindow] = Field(default_factory=FrozenList)
    observed_at: UTCDateTime
    source: str


class ModelTurn(SDKModel):
    text: str | None = None
    tool_calls: FrozenList[ToolCall] = Field(default_factory=FrozenList)
    finish: Literal["stop", "tool_calls", "cancelled", "error", "deadline"]
    usage: TokenUsage
    quota: QuotaSnapshot | None = None
    error: ErrorInfo | None = None


class Invocation(SDKModel):
    companion_id: str
    actor: Actor
    conversation: Conversation
    trace_id: TraceId
    task_id: TaskId | None = None


class Plan(SDKModel):
    summary: str
    heavy: bool
    data: JsonObject


class Denial(SDKModel):
    reason_code: str
    message_for_model: str


class ToolResult(SDKModel):
    call_id: str
    status: Literal["ok", "error", "denied", "cancelled"]
    content: str
    assets: FrozenList[AssetRef] = Field(default_factory=FrozenList)
    data: JsonObject
    error: ErrorInfo | None = None


class MemoryDecay(SDKModel):
    weight: float = Field(ge=0, le=1)
    frozen: bool


class MemoryRecord(SDKModel):
    record_id: TraceId
    revision: int = Field(ge=1)
    kind: Literal["fact", "preference", "event", "episode", "profile", "jargon"]
    content: str
    subject: str | None = None
    visibility: Visibility
    write_mode: Literal["auto", "confirmed"]
    source_type: Literal["private", "group", "owner_group_interaction"]
    source_ref: str | None = None
    scope: str | None = None
    created_at: UTCDateTime
    updated_at: UTCDateTime
    decay: MemoryDecay
    tags: FrozenList[str] = Field(default_factory=FrozenList)
    retracted: bool


class MemoryQuery(SDKModel):
    text: str
    scene: str
    allowed_visibility: FrozenList[Visibility] = Field(default_factory=FrozenList)
    scope: str | None = None
    limit: int


class MemoryHit(SDKModel):
    record_id: str
    score: float
    source: str


class BusEvent(SDKModel):
    name: str
    payload: JsonObject
    at: UTCDateTime
    source: str

    @model_validator(mode="after")
    def check_core_payload(self):
        payload_type = CORE_PAYLOAD_TYPES.get(self.name)
        if payload_type is not None:
            payload_type.model_validate(thaw(self.payload))
        return self


class Notice(SDKModel):
    level: Literal["info", "warn", "error"]
    title: str
    body: str
    dedupe_key: str


class Tick(SDKModel):
    schedule_id: str
    scheduled_at: UTCDateTime
    now: UTCDateTime
    missed: int


class ChannelFeatures(SDKModel):
    max_text_chars: int
    supports_react: bool
    supports_quote: bool
    supports_card: bool
    media_limits: JsonObject


class ModelFeatures(SDKModel):
    supports_tools: bool
    supports_images: bool
    context_chars: int
    agent_type: bool


class ReplyDraft(SDKModel):
    parts: FrozenList[MessagePart] = Field(default_factory=FrozenList)
    conversation: Conversation
    task_id: TaskId | None = None
    trace_id: TraceId
    form: Literal["plain", "split", "card"]
    quote: MessageRef | None = None


class CompanionState(SDKModel):
    companion_id: str
    states: FrozenDict[JsonObject]


class ContextRequest(SDKModel):
    scene: str
    actor: Actor
    budget_chars: int


class ContextSection(SDKModel):
    title: str
    text: str
    priority: int


class ApiRouteDeclaration(SDKModel):
    method: str
    path: Annotated[str, StringConstraints(pattern=r"^/plugins/[a-z][a-z0-9]*(?:-[a-z0-9]+)*/")]
    audit: bool | None = None
    handler: str

    @model_validator(mode="after")
    def check_audit(self):
        if self.method != "GET" and self.audit is not True:
            raise ValueError("non-GET plugin routes require audit=true")
        if any(part in {".", ".."} for part in self.path.split("/")):
            raise ValueError("plugin routes cannot contain relative path segments")
        return self


class WebuiPanelDeclaration(SDKModel):
    id: str
    title: str
    kind: Literal["kv", "table", "text"]
    data_route: str


class ServiceDeclaration(SDKModel):
    name: str
    version: str
    methods: FrozenList[str] = Field(default_factory=FrozenList)
    handler: str


class ScheduleDeclaration(SDKModel):
    id: str
    every: str | None = None
    at: str | None = None
    days: FrozenList[Annotated[int, Field(strict=True, ge=1, le=7)]] = Field(
        default_factory=FrozenList
    )
    handler: str

    @model_validator(mode="after")
    def check_schedule(self):
        if (self.every is None) == (self.at is None):
            raise ValueError("schedule requires exactly one of every or at")
        if self.at is not None and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.at):
            raise ValueError("at must be HH:MM")
        if self.every is not None:
            number = r"\d+(?:[.,]\d+)?"
            duration = (
                rf"P(?=\d|T\d)(?:{number}W|(?:{number}Y)?(?:{number}M)?(?:{number}D)?"
                rf"(?:T(?=\d)(?:{number}H)?(?:{number}M)?(?:{number}S)?)?)"
            )
            if not re.fullmatch(duration, self.every):
                raise ValueError("every must be an ISO 8601 duration")
            if self.days:
                raise ValueError("days belongs to an at schedule")
        return self


class ApiResponse(SDKModel):
    model_config = ConfigDict(serialize_by_alias=True)
    status: int
    json_value: FrozenJson | None = Field(default=None, alias="json")

    @property
    def json(self) -> FrozenJson | None:
        return self.json_value


class HttpResponse(SDKModel):
    status: int
    headers: FrozenDict[str]
    body: JsonBytes


class ProcessResult(SDKModel):
    returncode: int
    stdout: JsonBytes
    stderr: JsonBytes
    truncated: bool


class MemoryDraft(SDKModel):
    kind: Literal["fact", "preference", "event", "episode", "profile", "jargon"]
    content: str
    subject: str | None = None
    visibility: Visibility
    source_type: Literal["private", "group", "owner_group_interaction"]
    source_ref: str | None = None
    scope: str | None = None
    tags: FrozenList[str] = Field(default_factory=FrozenList)


class MessageReceivedPayload(SDKModel):
    event_id: str
    trace_id: TraceId
    conversation_id: str
    scene: str
    actor_id: str


class ReplySentPayload(SDKModel):
    trace_id: TraceId
    conversation_id: str
    intent_ids: FrozenList[TraceId] = Field(default_factory=FrozenList)
    task_id: TaskId | None = None


class TaskStartedPayload(SDKModel):
    task_id: TaskId
    trace_id: TraceId
    tool: str
    actor_id: str
    conversation_id: str


class TaskSummary(SDKModel):
    task_id: TaskId
    trace_id: TraceId
    tool: str
    status: Literal["ok", "error", "denied", "cancelled"]
    actor_id: str
    conversation_id: str
    scene: str
    started_at: UTCDateTime
    finished_at: UTCDateTime
    asset_ids: FrozenList[AssetId] = Field(default_factory=FrozenList)
    error_code: str | None = None


class CompanionStateChangedPayload(SDKModel):
    companion_id: str
    plugin_id: str
    state: JsonObject


class QuotaChangedPayload(SDKModel):
    model_plugin: str
    snapshot: QuotaSnapshot | None = None
    protection_mode: bool


class PluginChangedPayload(SDKModel):
    plugin_id: str
    companion_id: str
    reason: str | None = None


CORE_PAYLOAD_TYPES = {
    "message.received": MessageReceivedPayload,
    "reply.sent": ReplySentPayload,
    "task.started": TaskStartedPayload,
    "task.completed": TaskSummary,
    "task.failed": TaskSummary,
    "companion.state_changed": CompanionStateChangedPayload,
    "quota.changed": QuotaChangedPayload,
    "plugin.enabled": PluginChangedPayload,
    "plugin.disabled": PluginChangedPayload,
}
