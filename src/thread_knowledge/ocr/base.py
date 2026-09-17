from __future__ import annotations

from pathlib import Path
from typing import Protocol


class OcrEngine(Protocol):
    def extract_text(self, path: str | Path) -> str: ...


class NoopOcr:
    """Useful for tests and for running ingestion without OCR installed."""

    def extract_text(self, path: str | Path) -> str:
        return ""
