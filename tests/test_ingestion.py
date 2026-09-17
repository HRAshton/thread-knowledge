from datetime import datetime, timezone

from thread_knowledge.ingestion import build_search_document
from thread_knowledge.models import Attachment, RawMessage, RawThread


class FakeOcr:
    def extract_text(self, path):
        return f"OCR:{path}"


def test_build_search_document_keeps_thread_and_ocr():
    thread = RawThread(
        external_id="t1",
        source="fixture",
        messages=(
            RawMessage(
                external_id="m1",
                thread_id="t1",
                parent_id=None,
                author="alice",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                text="API is failing",
                title="Incident",
                attachments=(
                    Attachment(
                        kind="image",
                        name="error.png",
                        media_type="image/png",
                        local_path="/tmp/error.png",
                    ),
                ),
            ),
        ),
    )

    doc = build_search_document(thread, FakeOcr())

    assert doc["id"] == "t1"
    assert doc["messages"][0]["text"] == "API is failing"
    assert doc["attachments"][0]["ocr_text"] == "OCR:/tmp/error.png"
    assert doc["annotations"] == []
