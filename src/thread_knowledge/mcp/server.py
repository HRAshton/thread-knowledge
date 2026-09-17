from __future__ import annotations

from functools import lru_cache

from fastmcp import FastMCP

from thread_knowledge.models import AnnotationKind
from thread_knowledge.service import ThreadKnowledgeService
from thread_knowledge.storage.client import create_opensearch_client
from thread_knowledge.storage.opensearch import OpenSearchThreadStore

mcp = FastMCP("Thread Knowledge")


@lru_cache(maxsize=1)
def _service() -> ThreadKnowledgeService:
    store = OpenSearchThreadStore(create_opensearch_client())
    return ThreadKnowledgeService(store)


@mcp.tool
def search_threads(query: str, limit: int = 20) -> list[dict]:
    """
    Search historical discussion threads for information related to the current task.

    Use this before answering when previously discussed information may be relevant.
    Prefer concrete names, phrases, errors, identifiers, log fragments, and other
    distinctive evidence when available.

    Results are candidates, not conclusions. Review several plausible matches and
    use get_thread() to inspect promising discussions before relying on them.

    Args:
        query:
            Search expression describing the information to find.
        limit:
            Maximum candidate threads to return. Increase it when the query is broad
            or when finding multiple related discussions is important.

    Returns:
        Threads ranked by relevance, with matching message, OCR, or annotation
        fragments where available.
    """
    return _service().search_threads(query=query, limit=limit)


@mcp.tool
def get_thread(thread_id: str) -> dict | None:
    """
    Retrieve a complete discussion thread.

    Call this after search_threads() identifies a potentially relevant result. Use
    the complete discussion, attachments, OCR text, and annotations to understand
    context rather than relying only on search snippets.

    Args:
        thread_id:
            Thread identifier returned by search_threads().

    Returns:
        The complete thread with messages, attachments, OCR text, annotations,
        timestamps, and source metadata.
    """
    return _service().get_thread(thread_id)


@mcp.tool
def annotate_thread(
    thread_id: str,
    text: str,
    kind: AnnotationKind = AnnotationKind.SUMMARY,
) -> dict:
    """
    Attach durable AI-derived knowledge to an entire thread.

    Use this only when analysis produced concise information that can improve future
    retrieval or understanding. Suitable examples include a verified summary,
    conclusion, solution, or relationship to another discussion.

    Do not persist temporary reasoning, unsupported guesses, or redundant copies of
    information already explicit in the source. Write annotations so they remain
    understandable without the current chat context.
    """
    return _service().annotate_thread(thread_id=thread_id, text=text, kind=kind)


@mcp.tool
def annotate_item(
    thread_id: str,
    item_id: str,
    text: str,
    kind: AnnotationKind = AnnotationKind.SUMMARY,
) -> dict:
    """
    Attach durable AI-derived knowledge to a specific message or attachment.

    Use this when the new knowledge belongs to one concrete item, especially image
    interpretation beyond OCR or clarification of content in one message.

    The item must belong to the specified thread. Use get_thread() first to obtain
    valid message or attachment IDs. Do not store speculation or unrelated notes.
    """
    return _service().annotate_item(
        thread_id=thread_id,
        item_id=item_id,
        text=text,
        kind=kind,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
