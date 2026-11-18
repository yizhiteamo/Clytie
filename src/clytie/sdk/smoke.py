"""平台插件在冒烟测试命令行进程中实现的测试方协议。"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SmokeTarget:
    channel: str
    conversation: str
    companion_account: str
    tester_accounts: tuple[str, ...]
    allowed_accounts: frozenset[str]
    binding_config: Mapping[str, Any]
    reaction_symbols: Mapping[str, str]
    min_interval_seconds: float


@dataclass(frozen=True)
class SmokeReply:
    chars: int
    image: bool
    at: float


@dataclass(frozen=True)
class SmokeReaction:
    message_id: str
    state: str
    added: bool
    at: float


class SmokeDriverError(Exception):
    """仅固定错误码可进入终端和报告。"""

    def __init__(self, code: str):
        if code not in {
            "account_mismatch",
            "driver_closed",
            "event_overflow",
            "invalid_message",
            "invalid_ws",
            "member_mismatch",
            "protocol_failed",
            "send_unknown",
            "ws_closed",
            "ws_redirect",
        }:
            raise ValueError("invalid smoke driver error code")
        super().__init__(code)


class SmokeSecrets(Protocol):
    def read(self, path: str) -> str: ...


class SmokeDriver(Protocol):
    async def connect(self, config: Mapping[str, Any], secrets: SmokeSecrets) -> None: ...
    async def authorize(self, target: SmokeTarget) -> tuple[int, int]: ...
    async def send(self, case: Mapping[str, Any]) -> str: ...
    def events(self) -> AsyncIterator[SmokeReply | SmokeReaction]: ...
    async def close(self) -> None: ...
