from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from thread_knowledge.models import RawThread


class SourceConnector(Protocol):
    """Boundary between an external discussion system and the RAG pipeline."""

    async def iter_thread_ids(self, *, limit: int | None = None) -> AsyncIterator[str]: ...

    async def load_thread(self, thread_id: str) -> RawThread: ...
