from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


INDEX_MAPPING: dict[str, Any] = {
    "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "id": {"type": "keyword"},
            "source": {"type": "keyword"},
            "title": {"type": "text"},
            "source_url": {"type": "keyword", "index": False},
            "created_at": {"type": "date"},
            "messages": {
                "type": "nested",
                "properties": {
                    "id": {"type": "keyword"},
                    "parent_id": {"type": "keyword"},
                    "author": {"type": "keyword"},
                    "created_at": {"type": "date"},
                    "text": {"type": "text"},
                    "source_url": {"type": "keyword", "index": False},
                    "deleted": {"type": "boolean"},
                },
            },
            "attachments": {
                "type": "nested",
                "properties": {
                    "id": {"type": "keyword"},
                    "message_id": {"type": "keyword"},
                    "kind": {"type": "keyword"},
                    "name": {"type": "text"},
                    "media_type": {"type": "keyword"},
                    "url": {"type": "keyword", "index": False},
                    "local_path": {"type": "keyword", "index": False},
                    "ocr_text": {"type": "text"},
                },
            },
            "annotations": {
                "type": "nested",
                "properties": {
                    "id": {"type": "keyword"},
                    "target_type": {"type": "keyword"},
                    "item_id": {"type": "keyword"},
                    "kind": {"type": "keyword"},
                    "text": {"type": "text"},
                    "created_at": {"type": "date"},
                    "author": {"type": "keyword"},
                },
            },
        },
    },
}


class OpenSearchThreadStore:
    def __init__(self, client: Any, index: str = "thread-knowledge") -> None:
        self.client = client
        self.index = index

    def ensure_index(self) -> None:
        if not self.client.indices.exists(index=self.index):
            self.client.indices.create(index=self.index, body=INDEX_MAPPING)

    def upsert_thread(self, document: dict[str, Any]) -> None:
        self.client.index(
            index=self.index,
            id=document["id"],
            body=document,
            refresh=False,
        )

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        try:
            response = self.client.get(index=self.index, id=thread_id)
        except Exception as exc:
            # Avoid importing a concrete OpenSearch exception in the domain surface.
            if getattr(exc, "status_code", None) == 404:
                return None
            raise
        return response.get("_source")

    @staticmethod
    def lexical_query(query: str, limit: int) -> dict[str, Any]:
        """Thread-level BM25 search with exact matching fragments returned as inner hits."""
        return {
            "size": limit,
            "query": {
                "bool": {
                    "minimum_should_match": 1,
                    "should": [
                        {"match": {"title": {"query": query, "boost": 1.5}}},
                        {
                            "nested": {
                                "path": "messages",
                                "score_mode": "max",
                                "query": {"match": {"messages.text": query}},
                                "inner_hits": {
                                    "name": "matched_messages",
                                    "size": 5,
                                    "highlight": {"fields": {"messages.text": {}}},
                                },
                            }
                        },
                        {
                            "nested": {
                                "path": "attachments",
                                "score_mode": "max",
                                "query": {"match": {"attachments.ocr_text": query}},
                                "inner_hits": {
                                    "name": "matched_attachments",
                                    "size": 5,
                                    "highlight": {"fields": {"attachments.ocr_text": {}}},
                                },
                            }
                        },
                        {
                            "nested": {
                                "path": "annotations",
                                "score_mode": "max",
                                "query": {"match": {"annotations.text": query}},
                                "inner_hits": {
                                    "name": "matched_annotations",
                                    "size": 5,
                                    "highlight": {"fields": {"annotations.text": {}}},
                                },
                            }
                        },
                    ],
                }
            },
            "_source": ["id", "source", "title", "source_url", "created_at"],
        }

    def search_threads(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        response = self.client.search(
            index=self.index,
            body=self.lexical_query(query, limit),
        )
        results: list[dict[str, Any]] = []
        for hit in response.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            matched: list[dict[str, Any]] = []
            for group_name, group in hit.get("inner_hits", {}).items():
                for inner in group.get("hits", {}).get("hits", []):
                    inner_source = inner.get("_source", {})
                    highlights = [
                        fragment
                        for fragments in inner.get("highlight", {}).values()
                        for fragment in fragments
                    ]
                    matched.append(
                        {
                            "kind": group_name,
                            "score": inner.get("_score"),
                            "item": inner_source,
                            "highlights": highlights,
                        }
                    )
            results.append(
                {
                    **source,
                    "score": hit.get("_score"),
                    "matches": matched,
                }
            )
        return results

    def annotate_thread(
        self,
        thread_id: str,
        text: str,
        kind: str = "summary",
        author: str = "ai",
    ) -> dict[str, Any]:
        if self.get_thread(thread_id) is None:
            raise ValueError(f"Unknown thread_id: {thread_id}")
        return self._append_annotation(
            thread_id=thread_id,
            target_type="thread",
            item_id=thread_id,
            text=text,
            kind=kind,
            author=author,
        )

    def annotate_item(
        self,
        thread_id: str,
        item_id: str,
        text: str,
        kind: str = "finding",
        author: str = "ai",
    ) -> dict[str, Any]:
        thread = self.get_thread(thread_id)
        if thread is None:
            raise ValueError(f"Unknown thread_id: {thread_id}")

        target_type = None
        if any(message.get("id") == item_id for message in thread.get("messages", [])):
            target_type = "message"
        elif any(attachment.get("id") == item_id for attachment in thread.get("attachments", [])):
            target_type = "attachment"

        if target_type is None:
            raise ValueError(
                f"Unknown item_id {item_id!r} in thread {thread_id!r}; "
                "expected a message or attachment id returned by get_thread()."
            )

        return self._append_annotation(
            thread_id=thread_id,
            target_type=target_type,
            item_id=item_id,
            text=text,
            kind=kind,
            author=author,
        )

    def _append_annotation(
        self,
        thread_id: str,
        target_type: str,
        item_id: str,
        text: str,
        kind: str,
        author: str,
    ) -> dict[str, Any]:
        annotation = {
            "id": str(uuid4()),
            "target_type": target_type,
            "item_id": item_id,
            "kind": kind,
            "text": text,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "author": author,
        }
        self.client.update(
            index=self.index,
            id=thread_id,
            body={
                "script": {
                    "lang": "painless",
                    "source": (
                        "if (ctx._source.annotations == null) { ctx._source.annotations = []; } "
                        "ctx._source.annotations.add(params.annotation);"
                    ),
                    "params": {"annotation": annotation},
                }
            },
            refresh=True,
        )
        return annotation
