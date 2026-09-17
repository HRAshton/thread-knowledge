from __future__ import annotations

import os


def create_opensearch_client():
    try:
        from opensearchpy import OpenSearch
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Install project dependencies including opensearch-py") from exc

    url = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
    # Development compose uses security disabled. Production should configure TLS/auth.
    return OpenSearch(hosts=[url])
