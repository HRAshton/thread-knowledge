from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from thread_knowledge.models import Attachment, RawMessage, RawThread


class FixtureConnector:
    """Offline connector for deterministic threaded-discussion fixtures."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    async def iter_thread_ids(self, *, limit: int | None = None) -> AsyncIterator[str]:
        paths = sorted(self._directory.glob("*.json"))
        if limit is not None:
            paths = paths[:limit]
        for path in paths:
            yield path.stem

    async def load_thread(self, thread_id: str) -> RawThread:
        data = json.loads((self._directory / f"{thread_id}.json").read_text())
        messages = []
        for item in data["messages"]:
            messages.append(
                RawMessage(
                    external_id=item["external_id"],
                    thread_id=thread_id,
                    parent_id=item.get("parent_id"),
                    author=item.get("author"),
                    created_at=datetime.fromisoformat(item["created_at"]),
                    text=item.get("text", ""),
                    title=item.get("title"),
                    source_url=item.get("source_url"),
                    attachments=tuple(Attachment(**a) for a in item.get("attachments", [])),
                    deleted=item.get("deleted", False),
                )
            )
        return RawThread(external_id=thread_id, messages=tuple(messages), source="fixture")
