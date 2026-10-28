"""只使用内存和注入时钟的假实现，用于扩展点的离线契约验证。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from random import Random
from typing import Any, Literal

from clytie.sdk.cancellation import CancelToken
from clytie.sdk.clock import Clock
from clytie.sdk.testing.clock import MockClock
from clytie.sdk.testing.helpers import cancellable_wait
from clytie.sdk.types import (
    QUOTA_UNKNOWN,
    BusEvent,
    ChannelFeatures,
    ContextRequest,
    ContextSection,
    DeliveryIntent,
    EventEnvelope,
    FrozenDict,
    Invocation,
    MemoryHit,
    MemoryQuery,
    MemoryRecord,
    ModelFeatures,
    ModelRequest,
    ModelTurn,
    Notice,
    Plan,
    Receipt,
    Tick,
    TokenUsage,
    ToolCall,
    ToolResult,
)


@dataclass(frozen=True)
class FakeContext:
    """最小测试上下文；没有授权的可选属性不存在。

    尚未规定签名的日志、存储和事件接口仅放置不透明对象，不制造本体 API。
    """

    plugin_id: str = "fake-plugin"
    companion_id: str = "companion_a"
    config: Mapping[str, Any] = field(default_factory=FrozenDict)
    clock: Clock = field(default_factory=MockClock)
    random: Random = field(default_factory=lambda: Random(0))
    log: object = field(default_factory=object)
    storage: object = field(default_factory=object)
    events: object = field(default_factory=object)

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", FrozenDict(self.config))


class FakeModel:
    def __init__(self, ctx: FakeContext | None = None, *, delay: float = 0) -> None:
        self.ctx = ctx or FakeContext(plugin_id="fake-model")
        self.delay = delay
        self.requests: list[ModelRequest] = []

    async def generate(self, req: ModelRequest, cancel: CancelToken) -> ModelTurn:
        await cancellable_wait(self.ctx.clock, self.delay, cancel)
        self.requests.append(req)
        return ModelTurn(
            text="Fictional offline response.",
            finish="stop",
            usage=TokenUsage(input_tokens=0, output_tokens=0),
        )

    def features(self) -> ModelFeatures:
        return ModelFeatures(
            supports_tools=False,
            supports_images=False,
            context_chars=4096,
            agent_type=False,
        )

    async def quota(self) -> Literal["unknown"]:
        await self.ctx.clock.sleep(0)
        return QUOTA_UNKNOWN


class FakeChannel:
    def __init__(
        self,
        ctx: FakeContext | None = None,
        *,
        incoming: tuple[EventEnvelope, ...] = (),
        delay: float = 0,
    ) -> None:
        self.ctx = ctx or FakeContext(plugin_id="fake-channel")
        self.incoming = incoming
        self.delay = delay
        self.connected = False
        self.sent: list[DeliveryIntent] = []

    async def connect(self) -> None:
        await self.ctx.clock.sleep(0)
        self.connected = True

    async def close(self) -> None:
        await self.ctx.clock.sleep(0)
        self.connected = False

    async def events(self) -> AsyncIterator[EventEnvelope]:
        for event in self.incoming:
            await self.ctx.clock.sleep(0)
            yield event

    async def send(self, intent: DeliveryIntent, cancel: CancelToken) -> Receipt:
        await cancellable_wait(self.ctx.clock, self.delay, cancel)
        self.sent.append(intent)
        return Receipt(
            intent_id=intent.intent_id,
            status="sent",
            platform_message_id=f"fake-message-{len(self.sent)}",
            at=self.ctx.clock.now(),
        )

    async def react(self, intent: DeliveryIntent, cancel: CancelToken) -> Receipt:
        return await self.send(intent, cancel)

    def features(self) -> ChannelFeatures:
        return ChannelFeatures(
            max_text_chars=4096,
            supports_react=True,
            supports_quote=True,
            supports_card=True,
            media_limits={},
        )


class FakeEmbedder:
    dimension = 3

    def __init__(self, ctx: FakeContext | None = None) -> None:
        self.ctx = ctx or FakeContext()

    async def embed(self, texts: list[str], cancel: CancelToken) -> list[list[float]]:
        await cancellable_wait(self.ctx.clock, 0, cancel)
        return [[float(len(text)), float(text.count(" ")), 1.0] for text in texts]


class FakeCapability:
    def __init__(self, ctx: FakeContext | None = None) -> None:
        self.ctx = ctx or FakeContext()

    def idempotency_key(self, call: ToolCall, inv: Invocation) -> str:
        return f"{inv.companion_id}:{call.call_id}"

    async def prepare(self, call: ToolCall, inv: Invocation) -> Plan:
        await self.ctx.clock.sleep(0)
        return Plan(summary="Fictional tool operation", heavy=False, data={"call_id": call.call_id})

    async def execute(self, plan: Plan, inv: Invocation, cancel: CancelToken) -> ToolResult:
        await cancellable_wait(self.ctx.clock, 0, cancel)
        return ToolResult(
            call_id=plan.data["call_id"], status="ok", content="Fictional result.", data={}
        )


class FakeContextContributor:
    async def contribute(self, req: ContextRequest) -> ContextSection:
        return ContextSection(title="Fictional context", text="Imaginary setting.", priority=100)


class FakeScheduledHandler:
    def __init__(self) -> None:
        self.ticks: list[Tick] = []

    async def on_tick(self, tick: Tick) -> None:
        self.ticks.append(tick)


class FakeService:
    async def call(self, method: str, args: dict) -> dict:
        return {"method": method, "args": args}


class FakeEventSubscriber:
    def __init__(self) -> None:
        self.events: list[BusEvent] = []

    async def on_event(self, e: BusEvent) -> None:
        self.events.append(e)


class FakeCandidateReader:
    """尚未规定读取方法的协议占位；不模拟隐私投影 API。"""


class FakeRetriever:
    def __init__(self, ctx: FakeContext | None = None) -> None:
        self.ctx = ctx or FakeContext()

    async def search(
        self, q: MemoryQuery, candidates: object, cancel: CancelToken
    ) -> list[MemoryHit]:
        await cancellable_wait(self.ctx.clock, 0, cancel)
        # CandidateReader 的读取签名尚未规定；空结果不访问或泄露任何候选记录。
        return []


class FakeImporter:
    def __init__(self, records: tuple[MemoryRecord, ...], ctx: FakeContext | None = None) -> None:
        self.records = records
        self.ctx = ctx or FakeContext()

    def detect(self, source: Path) -> bool:
        return source.suffix == ".jsonl"

    async def read(self, source: Path, cancel: CancelToken) -> AsyncIterator[MemoryRecord]:
        cancel.raise_if_cancelled()
        for record in self.records:
            await cancellable_wait(self.ctx.clock, 0, cancel)
            yield record


class FakeConsolidator:
    def __init__(self, records: tuple[MemoryRecord, ...], ctx: FakeContext | None = None) -> None:
        self.records = records
        self.ctx = ctx or FakeContext()

    async def run(
        self,
        window_start: datetime,
        window_end: datetime,
        reader: object,
        cancel: CancelToken,
    ) -> list[MemoryRecord]:
        await cancellable_wait(self.ctx.clock, 0, cancel)
        return list(self.records)


class FakeNotifier:
    def __init__(self, ctx: FakeContext | None = None) -> None:
        self.ctx = ctx or FakeContext()

    async def notify(self, n: Notice, cancel: CancelToken) -> Receipt:
        await cancellable_wait(self.ctx.clock, 0, cancel)
        return Receipt(intent_id="d" * 32, status="sent", at=self.ctx.clock.now())


class FakeApiClient:
    """尚未规定客户端方法；CLI 假实现不执行本体业务。"""


class FakeCliCommand:
    async def run(self, args: dict, api: object) -> int:
        return 0
