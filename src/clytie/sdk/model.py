"""生成模型契约。"""

from typing import Literal, Protocol, runtime_checkable

from .cancellation import CancelToken
from .types import QUOTA_UNKNOWN, ModelFeatures, ModelRequest, ModelTurn, QuotaSnapshot

__all__ = ["QUOTA_UNKNOWN", "Model"]


@runtime_checkable
class Model(Protocol):
    async def generate(self, req: ModelRequest, cancel: CancelToken) -> ModelTurn: ...

    def features(self) -> ModelFeatures: ...

    async def quota(self) -> QuotaSnapshot | Literal["unknown"]: ...
