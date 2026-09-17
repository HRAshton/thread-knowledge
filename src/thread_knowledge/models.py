from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class Attachment:
    """Attachment referenced by a source message.

    ``local_path`` is populated by connectors/ingestion when bytes are available locally.
    ``url`` preserves source provenance. OCR output is stored in the search document, not
    in this raw source model.
    """

    kind: str
    url: str | None = None
    name: str | None = None
    media_type: str | None = None
    local_path: str | None = None
    external_id: str | None = None


@dataclass(frozen=True, slots=True)
class RawMessage:
    """Source-independent message representation."""

    external_id: str
    thread_id: str
    parent_id: str | None
    author: str | None
    created_at: datetime
    text: str
    title: str | None = None
    source_url: str | None = None
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class RawThread:
    external_id: str
    messages: tuple[RawMessage, ...]
    source: str = "unknown"

    @property
    def root(self) -> RawMessage:
        if not self.messages:
            raise ValueError("Thread has no messages")
        return self.messages[0]


class AnnotationKind(StrEnum):
    SUMMARY = "summary"
    SOLUTION = "solution"
    SCREENSHOT_ANALYSIS = "screenshot_analysis"
    RELATIONSHIP = "relationship"
    RELEVANCE_NOTE = "relevance_note"
    FINDING = "finding"


@dataclass(frozen=True, slots=True)
class Annotation:
    """AI-produced enrichment kept separate from source data."""

    annotation_id: str
    target_type: str
    item_id: str
    kind: str
    text: str
    created_at: datetime
    author: str = "ai"
