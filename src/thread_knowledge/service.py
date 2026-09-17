from __future__ import annotations

from typing import Any


class ThreadKnowledgeService:
    """Application API exposed to MCP and other clients."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def search_threads(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.search_threads(query=query, limit=limit)

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        return self.store.get_thread(thread_id)

    def annotate_thread(
        self,
        thread_id: str,
        text: str,
        kind: str = "summary",
    ) -> dict[str, Any]:
        return self.store.annotate_thread(
            thread_id=thread_id,
            text=text,
            kind=kind,
        )

    def annotate_item(
        self,
        thread_id: str,
        item_id: str,
        text: str,
        kind: str = "finding",
    ) -> dict[str, Any]:
        return self.store.annotate_item(
            thread_id=thread_id,
            item_id=item_id,
            text=text,
            kind=kind,
        )
