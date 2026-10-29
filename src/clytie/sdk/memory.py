"""记忆扩展契约；候选可见性由本体保证。"""

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from .cancellation import CancelToken
from .types import ImportCandidate, MemoryHit, MemoryQuery, MemoryRecord


class CandidateReader(Protocol):
    def iter(
        self,
        *,
        kinds: list[str] | None = None,
        scope: str | None = None,
        updated_after: datetime | None = None,
        batch: int = 200,
    ) -> AsyncIterator[list[MemoryRecord]]: ...
    async def get(self, record_ids: list[str]) -> list[MemoryRecord]: ...
    async def count(self) -> int: ...


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

    def read(self, source: Path, cancel: CancelToken) -> AsyncIterator[ImportCandidate]: ...


@runtime_checkable
class Reranker(Protocol):
    async def rerank(
        self, query: str, records: list[MemoryRecord], cancel: CancelToken
    ) -> list[float]: ...


@runtime_checkable
class Consolidator(Protocol):
    async def run(
        self,
        window_start: datetime,
        window_end: datetime,
        reader: CandidateReader,
        cancel: CancelToken,
    ) -> list[MemoryRecord]: ...
