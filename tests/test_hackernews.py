import httpx
import pytest

from thread_knowledge.connectors.hackernews import HackerNewsConnector, html_to_text


@pytest.mark.asyncio
async def test_loads_nested_thread_and_preserves_parent_ids():
    payloads = {
        "/v0/item/100.json": {
            "id": 100, "type": "story", "by": "alice", "time": 1700000000,
            "title": "SSO failure", "text": "After <b>upgrade</b>",
            "url": "https://example.test/incident", "kids": [101, 102],
        },
        "/v0/item/101.json": {
            "id": 101, "type": "comment", "by": "bob", "time": 1700000001,
            "parent": 100, "text": "Try version 5.2", "kids": [103],
        },
        "/v0/item/102.json": {
            "id": 102, "type": "comment", "by": "carol", "time": 1700000002,
            "parent": 100, "text": "I see E1042",
        },
        "/v0/item/103.json": {
            "id": 103, "type": "comment", "by": "alice", "time": 1700000003,
            "parent": 101, "text": "Fixed, thanks",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads[request.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = HackerNewsConnector(client=client)
        thread = await connector.load_thread("100")

    assert [m.external_id for m in thread.messages] == ["100", "101", "103", "102"]
    assert thread.messages[2].parent_id == "101"
    assert thread.root.title == "SSO failure"
    assert thread.root.text == "After upgrade"
    assert thread.root.attachments[0].url == "https://example.test/incident"


@pytest.mark.asyncio
async def test_iter_thread_ids_respects_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v0/newstories.json"
        return httpx.Response(200, json=[10, 11, 12])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = HackerNewsConnector(client=client)
        result = [x async for x in connector.iter_thread_ids(limit=2)]

    assert result == ["10", "11"]


def test_html_to_text():
    assert html_to_text("hello<p>world &amp; all") == "hello\nworld & all"
