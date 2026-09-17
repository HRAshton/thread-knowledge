from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from thread_knowledge.models import Attachment, RawThread
from thread_knowledge.ocr.base import OcrEngine

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}


def _is_image(attachment: Attachment) -> bool:
    if attachment.media_type and attachment.media_type.startswith("image/"):
        return True
    candidate = attachment.local_path or attachment.name or attachment.url or ""
    return Path(candidate).suffix.lower() in _IMAGE_EXTENSIONS


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def thread_fingerprint(thread: RawThread) -> str:
    """Stable fingerprint of source content, excluding local/cache-specific paths."""
    payload: dict[str, Any] = {
        "id": thread.external_id,
        "source": thread.source,
        "messages": [],
    }
    for message in thread.messages:
        attachments = []
        for index, attachment in enumerate(message.attachments):
            item: dict[str, Any] = {
                "id": attachment.external_id or f"{message.external_id}:{index}",
                "kind": attachment.kind,
                "name": attachment.name,
                "media_type": attachment.media_type,
            }
            # Prefer actual bytes when available. Browser URLs often contain rotating
            # auth/signature parameters and should not make unchanged threads look new.
            if attachment.local_path and Path(attachment.local_path).is_file():
                item["content_hash"] = _sha256_file(attachment.local_path)
            else:
                item["url"] = attachment.url
            attachments.append(item)

        payload["messages"].append(
            {
                "id": message.external_id,
                "parent_id": message.parent_id,
                "author": message.author,
                "created_at": message.created_at.isoformat(),
                "text": message.text,
                "title": message.title,
                "source_url": message.source_url,
                "deleted": message.deleted,
                "attachments": attachments,
            }
        )

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_search_document(
    thread: RawThread,
    ocr: OcrEngine,
    *,
    previous_document: dict[str, Any] | None = None,
    get_cached_ocr: Callable[[str], str | None] | None = None,
    cache_ocr: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Create one searchable parent document per thread.

    Existing AI annotations are preserved. OCR is reused by attachment content hash
    from either the previous thread document or the optional durable OCR cache.
    """

    previous_attachments = {
        attachment.get("id"): attachment
        for attachment in (previous_document or {}).get("attachments", [])
        if attachment.get("id")
    }

    attachments: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []

    for message in thread.messages:
        messages.append(
            {
                "id": message.external_id,
                "parent_id": message.parent_id,
                "author": message.author,
                "created_at": message.created_at.isoformat(),
                "text": message.text,
                "source_url": message.source_url,
                "deleted": message.deleted,
            }
        )
        for index, attachment in enumerate(message.attachments):
            attachment_id = attachment.external_id or f"{message.external_id}:{index}"
            content_hash = None
            ocr_text = ""

            if attachment.local_path and Path(attachment.local_path).is_file():
                content_hash = _sha256_file(attachment.local_path)

            if attachment.local_path and _is_image(attachment):
                previous = previous_attachments.get(attachment_id, {})
                ocr_cached = False
                if content_hash and previous.get("content_hash") == content_hash:
                    ocr_text = previous.get("ocr_text") or ""
                    ocr_cached = True
                elif content_hash and get_cached_ocr is not None:
                    cached = get_cached_ocr(content_hash)
                    if cached is not None:
                        ocr_text = cached
                        ocr_cached = True

                if not ocr_cached:
                    ocr_text = ocr.extract_text(attachment.local_path)
                    if content_hash and cache_ocr is not None:
                        cache_ocr(content_hash, ocr_text)

            attachments.append(
                {
                    "id": attachment_id,
                    "message_id": message.external_id,
                    "kind": attachment.kind,
                    "name": attachment.name,
                    "media_type": attachment.media_type,
                    "url": attachment.url,
                    "local_path": attachment.local_path,
                    "content_hash": content_hash,
                    "ocr_text": ocr_text,
                }
            )

    root = thread.root
    return {
        "id": thread.external_id,
        "source": thread.source,
        "title": root.title or root.text[:200],
        "source_url": root.source_url,
        "created_at": root.created_at.isoformat(),
        "content_hash": thread_fingerprint(thread),
        "messages": messages,
        "attachments": attachments,
        "annotations": list((previous_document or {}).get("annotations", [])),
    }


def ingest_thread(thread: RawThread, ocr: OcrEngine, store: Any) -> bool:
    """Index a thread only when source content changed.

    Returns ``True`` when OpenSearch was updated and ``False`` when the existing
    document has the same content fingerprint.
    """
    fingerprint = thread_fingerprint(thread)
    previous = store.get_thread(thread.external_id)
    if previous is not None and previous.get("content_hash") == fingerprint:
        return False

    document = build_search_document(
        thread,
        ocr,
        previous_document=previous,
        get_cached_ocr=getattr(store, "get_cached_ocr", None),
        cache_ocr=getattr(store, "cache_ocr", None),
    )
    store.upsert_thread(document)
    return True
