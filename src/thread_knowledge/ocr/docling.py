from __future__ import annotations

from pathlib import Path


class DoclingOcr:
    """Local OCR adapter backed by Docling.

    Docling is imported lazily because its OCR stack is intentionally an optional,
    heavyweight dependency. No remote AI service is used by this adapter.
    """

    def __init__(self) -> None:
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise RuntimeError(
                "Docling OCR is not installed. Install thread-knowledge[ocr]."
            ) from exc
        self._converter = DocumentConverter()

    def extract_text(self, path: str | Path) -> str:
        result = self._converter.convert(Path(path))
        # export_to_text returns Docling's normalized text, including OCR text for images.
        return result.document.export_to_text().strip()
