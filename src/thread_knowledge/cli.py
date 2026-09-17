from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from thread_knowledge.connectors.fixture import FixtureConnector
from thread_knowledge.connectors.hackernews import HackerNewsConnector
from thread_knowledge.ingestion import build_search_document
from thread_knowledge.ocr.base import NoopOcr
from thread_knowledge.ocr.docling import DoclingOcr
from thread_knowledge.storage.client import create_opensearch_client
from thread_knowledge.storage.opensearch import OpenSearchThreadStore


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _ocr_engine(name: str):
    if name == "none":
        return NoopOcr()
    if name == "docling":
        return DoclingOcr()
    raise ValueError(f"Unknown OCR engine: {name}")


async def export_hn(limit: int, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    async with HackerNewsConnector() as connector:
        async for thread_id in connector.iter_thread_ids(limit=limit):
            thread = await connector.load_thread(thread_id)
            path = output / f"{thread_id}.json"
            path.write_text(
                json.dumps(asdict(thread), default=_json_default, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"exported {thread_id}: {len(thread.messages)} messages -> {path}")


async def index_connector(connector, *, limit: int | None, ocr_name: str) -> None:
    store = OpenSearchThreadStore(create_opensearch_client())
    store.ensure_index()
    ocr = _ocr_engine(ocr_name)
    count = 0
    async for thread_id in connector.iter_thread_ids(limit=limit):
        thread = await connector.load_thread(thread_id)
        document = build_search_document(thread, ocr)
        store.upsert_thread(document)
        count += 1
        print(f"indexed {thread_id}: {len(thread.messages)} messages")
    print(f"indexed {count} threads")


async def index_hn(limit: int, ocr_name: str) -> None:
    async with HackerNewsConnector() as connector:
        await index_connector(connector, limit=limit, ocr_name=ocr_name)


async def index_fixtures(directory: Path, limit: int | None, ocr_name: str) -> None:
    connector = FixtureConnector(directory)
    await index_connector(connector, limit=limit, ocr_name=ocr_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Thread knowledge ingestion/search")
    sub = parser.add_subparsers(dest="command", required=True)

    hn = sub.add_parser("hn", help="Export Hacker News threads to JSON")
    hn.add_argument("--limit", type=int, default=10)
    hn.add_argument("--output", type=Path, default=Path("data/hn"))

    init_index = sub.add_parser("init-index", help="Create the OpenSearch index")

    index_hn_cmd = sub.add_parser("index-hn", help="Ingest Hacker News directly into OpenSearch")
    index_hn_cmd.add_argument("--limit", type=int, default=20)
    index_hn_cmd.add_argument("--ocr", choices=["none", "docling"], default="none")

    index_fixture_cmd = sub.add_parser("index-fixtures", help="Ingest fixture threads into OpenSearch")
    index_fixture_cmd.add_argument("directory", type=Path)
    index_fixture_cmd.add_argument("--limit", type=int)
    index_fixture_cmd.add_argument("--ocr", choices=["none", "docling"], default="none")

    args = parser.parse_args()

    if args.command == "hn":
        asyncio.run(export_hn(args.limit, args.output))
    elif args.command == "init-index":
        store = OpenSearchThreadStore(create_opensearch_client())
        store.ensure_index()
        print(f"index ready: {store.index}")
    elif args.command == "index-hn":
        asyncio.run(index_hn(args.limit, args.ocr))
    elif args.command == "index-fixtures":
        asyncio.run(index_fixtures(args.directory, args.limit, args.ocr))


if __name__ == "__main__":
    main()
