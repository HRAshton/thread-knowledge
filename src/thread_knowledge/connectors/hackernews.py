from __future__ import annotations

import asyncio
import html
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import httpx

from thread_knowledge.models import Attachment, RawMessage, RawThread

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<(?:p|br)\s*/?>", re.IGNORECASE)


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    value = _BREAK_RE.sub("\n", value)
    value = _TAG_RE.sub("", value)
    value = html.unescape(value)
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


class HackerNewsConnector:
    """Public Hacker News adapter used as a realistic threaded-discussion source.

    Story -> thread root; comments -> replies; nested comments preserve parent_id.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = "https://hacker-news.firebaseio.com/v0",
        feed: str = "newstories",
        concurrency: int = 20,
    ) -> None:
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._base_url = base_url.rstrip("/")
        self._feed = feed
        self._sem = asyncio.Semaphore(concurrency)

    async def __aenter__(self) -> "HackerNewsConnector":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def _get_json(self, path: str) -> Any:
        async with self._sem:
            response = await self._client.get(f"{self._base_url}/{path.lstrip('/')}")
            response.raise_for_status()
            return response.json()

    async def iter_thread_ids(self, *, limit: int | None = None) -> AsyncIterator[str]:
        ids: list[int] = await self._get_json(f"{self._feed}.json")
        selected = ids if limit is None else ids[:limit]
        for item_id in selected:
            yield str(item_id)

    async def _load_item(self, item_id: str | int) -> dict[str, Any] | None:
        return await self._get_json(f"item/{item_id}.json")

    async def load_thread(self, thread_id: str) -> RawThread:
        root = await self._load_item(thread_id)
        if not root or root.get("type") != "story":
            raise ValueError(f"HN item {thread_id} is missing or is not a story")

        messages: list[RawMessage] = [self._to_message(root, thread_id=thread_id, parent_id=None)]
        await self._walk_comments(root.get("kids", []), thread_id, messages)
        return RawThread(external_id=thread_id, messages=tuple(messages), source="hackernews")

    async def _walk_comments(
        self,
        ids: list[int],
        thread_id: str,
        out: list[RawMessage],
    ) -> None:
        # Fetch siblings concurrently, but append them in API order for deterministic output.
        items = await asyncio.gather(*(self._load_item(item_id) for item_id in ids))
        for item in items:
            if not item:
                continue
            parent_id = str(item.get("parent")) if item.get("parent") is not None else None
            out.append(self._to_message(item, thread_id=thread_id, parent_id=parent_id))
            kids = item.get("kids", [])
            if kids:
                await self._walk_comments(kids, thread_id, out)

    def _to_message(
        self,
        item: dict[str, Any],
        *,
        thread_id: str,
        parent_id: str | None,
    ) -> RawMessage:
        item_id = str(item["id"])
        item_url = f"https://news.ycombinator.com/item?id={item_id}"
        external_url = item.get("url")
        attachments = (
            (Attachment(kind="link", url=external_url),) if external_url else ()
        )
        return RawMessage(
            external_id=item_id,
            thread_id=thread_id,
            parent_id=parent_id,
            author=item.get("by"),
            created_at=datetime.fromtimestamp(item.get("time", 0), tz=timezone.utc),
            text=html_to_text(item.get("text")),
            title=html_to_text(item.get("title")) or None,
            source_url=item_url,
            attachments=attachments,
            deleted=bool(item.get("deleted") or item.get("dead")),
        )
