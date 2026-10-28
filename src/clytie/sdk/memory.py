"""记忆扩展契约；候选可见性由本体保证。"""

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from .cancellation import CancelToken
from .types import MemoryHit, MemoryQuery, MemoryRecord


class CandidateReader(Protocol):
    """候选读取能力边界；具体读取方法尚未由规格定义。"""


@runtime_checkable
class Embedder(Protocol):
    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: list[str], cancel: CancelToken) -> list[list[float]]: ...


@runtime_checkable
class Retriever(Protocol):
    async def search(
        self, q: MemoryQuery, candidates: CandidateReader, cancel: CancelToken
    ) -> list[MemoryHit]: ...


@runtime_checkable
class Importer(Protocol):
    def detect(self, source: Path) -> bool: ...

    def read(self, source: Path, cancel: CancelToken) -> AsyncIterator[MemoryRecord]: ...


@runtime_checkable
class Consolidator(Protocol):
    async def run(
        self,
        window_start: datetime,
        window_end: datetime,
        reader: CandidateReader,
        cancel: CancelToken,
    ) -> list[MemoryRecord]: ...
