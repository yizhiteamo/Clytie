"""在线备份协议；ticket 与 view 句柄仅在当前提供者进程内有效。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated, Protocol, runtime_checkable

from pydantic import Field, StringConstraints, field_validator

from .cancellation import CancelToken
from .types import AssetRef, SDKModel, UTCDateTime

Nonempty = Annotated[str, StringConstraints(min_length=1)]


class BackupRequest(SDKModel):
    operation_id: Nonempty
    companion_id: Nonempty
    plugin_id: Nonempty
    data_version: int = Field(strict=True, ge=1)
    deadline: UTCDateTime


@dataclass(frozen=True)
class BackupTicket:
    """提供者的不透明准备资源，不可持久化或跨 IPC 传输。"""

    handle: object = field(repr=False)

    def __reduce_ex__(self, protocol):
        raise TypeError("backup tickets cannot be serialized")


@dataclass(frozen=True)
class BackupView:
    """稳定视图的元数据；handle 的只读性由提供者契约保证。"""

    epoch: str
    data_version: int
    revision: str
    handle: object = field(repr=False)
    assets: tuple[AssetRef, ...] = ()

    def __post_init__(self):
        if not isinstance(self.epoch, str) or not self.epoch:
            raise ValueError("epoch must be a nonempty string")
        if type(self.data_version) is not int or self.data_version < 1:
            raise ValueError("data_version must be a positive integer")
        if not isinstance(self.revision, str) or not self.revision:
            raise ValueError("revision must be a nonempty string")
        if not isinstance(self.assets, (tuple, list)) or any(
            not isinstance(asset, AssetRef) for asset in self.assets
        ):
            raise ValueError("assets must contain AssetRef values")
        object.__setattr__(
            self,
            "assets",
            tuple(AssetRef.model_validate(asset.model_dump()) for asset in self.assets),
        )

    def __reduce_ex__(self, protocol):
        raise TypeError("backup views cannot be serialized")


class BackupEntry(SDKModel):
    relative_path: Nonempty
    size: int = Field(strict=True, ge=0)
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value):
        if (
            "\\" in value
            or ":" in value
            or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("expected a canonical relative member path")
        return value


@runtime_checkable
class BackupSink(Protocol):
    async def write(self, relative_path: str, chunks: AsyncIterator[bytes]) -> BackupEntry: ...


@runtime_checkable
class BackupProvider(Protocol):
    async def prepare(self, request: BackupRequest, cancel: CancelToken) -> BackupTicket: ...

    async def capture(
        self, ticket: BackupTicket, epoch: str, cancel: CancelToken
    ) -> BackupView: ...

    async def materialize(
        self, view: BackupView, sink: BackupSink, cancel: CancelToken
    ) -> None: ...

    async def release(self, ticket: BackupTicket) -> None: ...
