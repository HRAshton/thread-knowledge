from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from thread_knowledge.models import Attachment, RawThread
from thread_knowledge.ocr.base import OcrEngine

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}


def _is_image(attachment: Attachment) -> bool:
    if attachment.media_type and attachment.media_type.startswith("image/"):
        return True
    candidate = attachment.local_path or attachment.name or attachment.url or ""
    return Path(candidate).suffix.lower() in _IMAGE_EXTENSIONS


def build_search_document(thread: RawThread, ocr: OcrEngine) -> dict[str, Any]:
    """Create one lossless-ish searchable parent document per thread.

    Source messages remain separate nested objects, letting OpenSearch return the exact
    matching reply via inner_hits while ranking the parent discussion.
    """

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
            ocr_text = ""
            if attachment.local_path and _is_image(attachment):
                ocr_text = ocr.extract_text(attachment.local_path)
            attachments.append(
                {
                    "id": attachment_id,
                    "message_id": message.external_id,
                    "kind": attachment.kind,
                    "name": attachment.name,
                    "media_type": attachment.media_type,
                    "url": attachment.url,
                    "local_path": attachment.local_path,
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
        "messages": messages,
        "attachments": attachments,
        "annotations": [],
    }
