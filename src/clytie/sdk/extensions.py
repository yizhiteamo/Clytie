"""其他已明确签名的扩展点。"""

from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from .cancellation import CancelToken
from .context import ServiceCall
from .types import (
    ApiResponse,
    BusEvent,
    ContextRequest,
    ContextSection,
    JsonObject,
    Notice,
    Receipt,
    Tick,
)


class ApiClient(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        *,
        json: JsonObject | None = None,
        query: dict[str, str] | None = None,
    ) -> ApiResponse: ...


@runtime_checkable
class ContextContributor(Protocol):
    async def contribute(self, req: ContextRequest) -> ContextSection | None: ...


@runtime_checkable
class ScheduledHandler(Protocol):
    async def on_tick(self, tick: Tick) -> None: ...


@runtime_checkable
class Service(Protocol):
    async def call(self, method: str, args: dict[str, JsonValue]) -> dict[str, JsonValue]: ...


class ContextService(Protocol):
    async def call(
        self, method: str, args: dict[str, JsonValue], call: ServiceCall
    ) -> dict[str, JsonValue]: ...


@runtime_checkable
class EventSubscriber(Protocol):
    async def on_event(self, e: BusEvent) -> None: ...


@runtime_checkable
class Notifier(Protocol):
    async def notify(self, n: Notice, cancel: CancelToken) -> Receipt: ...


@runtime_checkable
class CliCommand(Protocol):
    async def run(self, args: dict[str, JsonValue], api: ApiClient) -> int: ...
