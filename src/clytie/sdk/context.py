"""上下文方法协议；可选属性由宿主按权限提供，未授权时不存在。"""

from pathlib import Path
from random import Random
from typing import Protocol

from .cancellation import CancelToken
from .clock import Clock
from .types import (
    Actor,
    AssetRef,
    Conversation,
    HttpResponse,
    JsonObject,
    JsonValue,
    MemoryDraft,
    MemoryRecord,
    MessagePart,
    ModelRequest,
    ModelTurn,
    ProactiveOutcome,
    ProactiveSubmission,
    ProcessResult,
)


class RevisionConflict(Exception):
    """调用方修订号已过期。"""


class ServiceUnavailable(Exception):
    """服务当前不可用。"""


class CredentialUnavailable(Exception):
    """凭据不可用；异常不包含路径或凭据内容。"""

    def __init__(self, code):
        if code not in {"unknown_ref", "not_permitted", "missing", "empty", "unreadable"}:
            raise ValueError("invalid credential error code")
        self.code = code
        super().__init__(code)


class Credentials(Protocol):
    async def read(self, ref: str) -> str: ...


class ChannelCredentials(Credentials, Protocol):
    async def account_secret(self) -> str: ...


class WebSocketClosed(Exception):
    def __init__(self, close_code: int | None = None):
        self.close_code = close_code
        super().__init__("websocket_closed")


class WebSocketConnection(Protocol):
    async def send_text(self, text: str) -> None: ...
    async def receive(self, cancel: CancelToken) -> str: ...
    async def close(self) -> None: ...
    @property
    def closed(self) -> bool: ...
    @property
    def close_code(self) -> int | None: ...


class WebSocket(Protocol):
    async def connect(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout_s: float = 10,
        max_message_bytes: int = 16 * 1024 * 1024,
    ) -> WebSocketConnection: ...


class Logger(Protocol):
    def debug(self, msg: str, **fields: JsonValue) -> None: ...
    def info(self, msg: str, **fields: JsonValue) -> None: ...
    def warning(self, msg: str, **fields: JsonValue) -> None: ...
    def error(self, msg: str, **fields: JsonValue) -> None: ...


class Storage(Protocol):
    @property
    def path(self) -> Path: ...
    async def get(self, key: str) -> JsonValue | None: ...
    async def set(self, key: str, value: JsonValue) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def keys(self, prefix: str = "") -> list[str]: ...


class Events(Protocol):
    async def publish(self, name: str, payload: JsonObject) -> None: ...


class State(Protocol):
    async def set(self, payload: JsonObject) -> None: ...


class Assets(Protocol):
    async def info(self, asset_id: str) -> AssetRef: ...
    async def read(self, asset_id: str) -> bytes: ...
    async def path(self, asset_id: str) -> Path: ...


class AssetWriter(Protocol):
    async def put(self, data: bytes, media_type: str, origin: str) -> AssetRef: ...


class MemoryView(Protocol):
    async def search(self, text: str, *, limit: int = 10) -> list[MemoryRecord]: ...
    async def get(self, record_id: str) -> MemoryRecord | None: ...


class MemoryWriter(Protocol):
    async def write(self, draft: MemoryDraft) -> MemoryRecord: ...


class WritableMemoryView(MemoryView, MemoryWriter, Protocol):
    async def revise(
        self, record_id: str, expected_revision: int, changes: JsonObject
    ) -> MemoryRecord: ...
    async def retract(self, record_id: str, expected_revision: int, reason: str) -> None: ...


class ServiceProxy(Protocol):
    async def call(self, method: str, args: JsonObject) -> JsonObject: ...


class Services(Protocol):
    def get(self, name: str) -> ServiceProxy | None: ...


class Peer(Protocol):
    async def send(self, companion_id: str, kind: str, payload: JsonObject) -> str: ...
    async def request(
        self, companion_id: str, kind: str, payload: JsonObject, timeout_s: float
    ) -> JsonObject: ...


class ModelInvoker(Protocol):
    async def generate(
        self, req: ModelRequest, cancel: CancelToken, *, purpose: str
    ) -> ModelTurn: ...


class Http(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout_s: float = 30,
        max_bytes: int = 20 * 1024 * 1024,
        follow_redirects: bool = True,
    ) -> HttpResponse: ...


class Proactive(Protocol):
    async def submit(
        self,
        conversation_id: str,
        *,
        topic: str | None = None,
        material: JsonObject | None = None,
        parts: list[MessagePart] | None = None,
        reason: str,
        tools: bool = False,
    ) -> ProactiveSubmission: ...

    async def result(self, trace_id: str) -> ProactiveOutcome: ...


def validate_proactive(*, topic=None, material=None, parts=None, tools=False):
    """共享 SDK 参数约束；目标会话与场景仍由本体验证。"""
    if not isinstance(tools, bool):
        raise ValueError("tools must be a boolean")
    if (topic is None) == (parts is None):
        raise ValueError("provide exactly one of topic or parts")
    if topic is not None and not isinstance(topic, str):
        raise ValueError("topic must be text")
    if parts is not None and any(
        p.type not in {"sticker", "image", "audio", "video", "file"} for p in parts
    ):
        raise ValueError("text requires topic mode")


class Subprocess(Protocol):
    async def run(
        self,
        args: list[str],
        *,
        timeout_s: float,
        env: dict[str, str] | None = None,
        max_output_bytes: int = 10 * 1024 * 1024,
    ) -> ProcessResult: ...


class Identity(Protocol):
    def self_account(self) -> str | None: ...
    def resolve_actor(self, account: str) -> Actor: ...
    def resolve_conversation(self, kind: str, platform_id: str) -> Conversation: ...
    def account_for(self, actor_id: str) -> str | None: ...
    def platform_id_for(self, conversation_id: str) -> str | None: ...


class PluginContext(Protocol):
    @property
    def plugin_id(self) -> str: ...
    @property
    def companion_id(self) -> str: ...
    @property
    def config(self) -> JsonObject: ...
    @property
    def log(self) -> Logger: ...
    @property
    def clock(self) -> Clock: ...
    @property
    def random(self) -> Random: ...
    @property
    def storage(self) -> Storage: ...
    @property
    def events(self) -> Events: ...
    @property
    def state(self) -> State: ...
    @property
    def assets(self) -> Assets: ...
