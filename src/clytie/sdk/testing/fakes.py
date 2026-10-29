"""只使用内存和注入时钟的假实现，用于扩展点的离线契约验证。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Literal

from clytie.sdk.cancellation import CancelToken
from clytie.sdk.testing.helpers import cancellable_wait
from clytie.sdk.types import (
    QUOTA_UNKNOWN,
    BusEvent,
    ChannelFeatures,
    ContextRequest,
    ContextSection,
    DeliveryIntent,
    EventEnvelope,
    ImportCandidate,
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

from .context_fakes import FakeApiClient as FakeApiClient
from .context_fakes import FakeCandidateReader as FakeCandidateReader
from .context_fakes import FakeContext


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


class FakeRetriever:
    def __init__(self, ctx: FakeContext | None = None) -> None:
        self.ctx = ctx or FakeContext()

    async def search(
        self, q: MemoryQuery, candidates: object, cancel: CancelToken
    ) -> list[MemoryHit]:
        await cancellable_wait(self.ctx.clock, 0, cancel)
        hits = []
        async for batch in candidates.iter(scope=q.scope):
            cancel.raise_if_cancelled()
            for record in batch:
                if q.text in record.content:
                    hits.append(
                        MemoryHit(record_id=record.record_id, score=1, source="fake-retriever")
                    )
        return hits[: q.limit]


class FakeImporter:
    def __init__(
        self, records: tuple[ImportCandidate, ...], ctx: FakeContext | None = None
    ) -> None:
        self.records = records
        self.ctx = ctx or FakeContext()

    def detect(self, source: Path) -> bool:
        return source.suffix == ".jsonl"

    async def read(self, source: Path, cancel: CancelToken) -> AsyncIterator[ImportCandidate]:
        cancel.raise_if_cancelled()
        for record in self.records:
            await cancellable_wait(self.ctx.clock, 0, cancel)
            yield record


class FakeReranker:
    def __init__(self, ctx: FakeContext | None = None):
        self.ctx = ctx or FakeContext()

    async def rerank(self, query, records, cancel):
        await cancellable_wait(self.ctx.clock, 0, cancel)
        return [float(query in record.content) for record in records]


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


class FakeCliCommand:
    async def run(self, args: dict, api: object) -> int:
        response = await api.request("GET", "/api/v1/status")
        return 0 if response.status == 200 else 1
