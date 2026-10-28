"""其他已明确签名的扩展点。"""

from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from .cancellation import CancelToken
from .types import BusEvent, ContextRequest, ContextSection, Notice, Receipt, Tick


class ApiClient(Protocol):
    """CLI 调用 API 的边界；具体操作在管理接口里程碑定义。"""


@runtime_checkable
class ContextContributor(Protocol):
    async def contribute(self, req: ContextRequest) -> ContextSection | None: ...


@runtime_checkable
class ScheduledHandler(Protocol):
    async def on_tick(self, tick: Tick) -> None: ...


@runtime_checkable
class Service(Protocol):
    async def call(self, method: str, args: dict[str, JsonValue]) -> dict[str, JsonValue]: ...


@runtime_checkable
class EventSubscriber(Protocol):
    async def on_event(self, e: BusEvent) -> None: ...


@runtime_checkable
class Notifier(Protocol):
    async def notify(self, n: Notice, cancel: CancelToken) -> Receipt: ...


@runtime_checkable
class CliCommand(Protocol):
    async def run(self, args: dict[str, JsonValue], api: ApiClient) -> int: ...
