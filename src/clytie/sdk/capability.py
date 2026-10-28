"""能力契约；执行结果只能返回给本体，不能直接交付。"""

from typing import Protocol, runtime_checkable

from .cancellation import CancelToken
from .types import Denial, Invocation, Plan, ToolCall, ToolResult


@runtime_checkable
class Capability(Protocol):
    def idempotency_key(self, call: ToolCall, inv: Invocation) -> str: ...

    async def prepare(self, call: ToolCall, inv: Invocation) -> Plan | Denial: ...

    async def execute(self, plan: Plan, inv: Invocation, cancel: CancelToken) -> ToolResult: ...
