from thread_knowledge.storage.opensearch import OpenSearchThreadStore


class FakeIndices:
    def __init__(self):
        self.created = None

    def exists(self, index):
        return False

    def create(self, index, body):
        self.created = (index, body)


class FakeClient:
    def __init__(self):
        self.indices = FakeIndices()
        self.last_search = None
        self.last_update = None
        self.documents = {
            "t1": {
                "id": "t1",
                "messages": [{"id": "m2", "text": "resolver timeout"}],
                "attachments": [{"id": "a1", "ocr_text": "connection refused"}],
                "annotations": [],
            }
        }

    def search(self, index, body):
        self.last_search = (index, body)
        return {
            "hits": {
                "hits": [
                    {
                        "_score": 4.2,
                        "_source": {"id": "t1", "title": "DNS timeout"},
                        "inner_hits": {
                            "matched_messages": {
                                "hits": {
                                    "hits": [
                                        {
                                            "_score": 3.0,
                                            "_source": {"id": "m2", "text": "resolver timeout"},
                                            "highlight": {"messages.text": ["resolver <em>timeout</em>"]},
                                        }
                                    ]
                                }
                            }
                        },
                    }
                ]
            }
        }

    def get(self, index, id):
        if id not in self.documents:
            exc = RuntimeError("not found")
            exc.status_code = 404
            raise exc
        return {"_source": self.documents[id]}

    def update(self, index, id, body, refresh):
        self.last_update = (index, id, body, refresh)


def test_search_returns_parent_and_matching_fragment():
    client = FakeClient()
    store = OpenSearchThreadStore(client)
    results = store.search_threads("resolver timeout", limit=10)

    assert results[0]["id"] == "t1"
    assert results[0]["matches"][0]["item"]["id"] == "m2"
    _, query = client.last_search
    nested_paths = [
        clause.get("nested", {}).get("path")
        for clause in query["query"]["bool"]["should"]
        if "nested" in clause
    ]
    assert nested_paths == ["messages", "attachments", "annotations"]


def test_annotate_thread_targets_thread():
    client = FakeClient()
    store = OpenSearchThreadStore(client)
    annotation = store.annotate_thread("t1", "Known DNS outage", kind="summary")

    assert annotation["target_type"] == "thread"
    assert annotation["item_id"] == "t1"
    assert client.last_update[1] == "t1"


def test_annotate_item_validates_and_records_message_target():
    client = FakeClient()
    store = OpenSearchThreadStore(client)
    annotation = store.annotate_item("t1", "m2", "This reply confirms resolver failure")

    assert annotation["target_type"] == "message"
    assert annotation["item_id"] == "m2"
    assert client.last_update[2]["script"]["params"]["annotation"]["text"] == "This reply confirms resolver failure"


def test_annotate_item_records_attachment_target():
    client = FakeClient()
    store = OpenSearchThreadStore(client)
    annotation = store.annotate_item("t1", "a1", "Screenshot shows repeated connection refusal", kind="screenshot_analysis")

    assert annotation["target_type"] == "attachment"
    assert annotation["item_id"] == "a1"
