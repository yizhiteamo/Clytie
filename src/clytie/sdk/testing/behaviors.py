"""行为环节的中性假实现，不执行发送或账本操作。"""

from clytie.sdk.behavior import Normal, Respond
from clytie.sdk.types import (
    CompanionState,
    DeliveryIntent,
    EventEnvelope,
    Message,
    ReplyDraft,
    TaskSummary,
)

from .fakes import FakeContext


class _FakeBehavior:
    def __init__(self, ctx: FakeContext | None = None) -> None:
        self.ctx = ctx or FakeContext()


class FakeMessageReceived(_FakeBehavior):
    async def __call__(self, input: EventEnvelope) -> None:
        await self.ctx.clock.sleep(0)


class FakeBeforeAttention(_FakeBehavior):
    async def __call__(self, input: tuple[EventEnvelope, CompanionState]) -> Normal:
        await self.ctx.clock.sleep(0)
        return Normal()


class FakeAttention(_FakeBehavior):
    async def __call__(self, input: tuple[EventEnvelope, list[Message]]) -> Respond:
        await self.ctx.clock.sleep(0)
        return Respond()


class FakeAfterGeneration(_FakeBehavior):
    async def __call__(self, input: ReplyDraft) -> ReplyDraft:
        await self.ctx.clock.sleep(0)
        return input


class FakeBeforeSend(_FakeBehavior):
    async def __call__(self, input: list[DeliveryIntent]) -> list[DeliveryIntent]:
        await self.ctx.clock.sleep(0)
        return input


class FakeTaskCompleted(_FakeBehavior):
    async def __call__(self, input: TaskSummary) -> None:
        await self.ctx.clock.sleep(0)
