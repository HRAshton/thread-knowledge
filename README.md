# Thread Knowledge MCP

Local-first retrieval and enrichment for threaded discussions with screenshots analysis.

The service ingests discussion threads, extracts text from images with local OCR,
indexes content in OpenSearch, and exposes retrieval and annotation tools through MCP.

## Architecture

```text
Connector
   ↓
normalized threads
   ↓
local OCR
   ↓
OpenSearch
   ↓
MCP
 ├─ search_threads
 ├─ get_thread
 ├─ annotate_thread
 └─ annotate_item
   ↓
AI client
```

AI annotations are stored separately from source content and become searchable
on subsequent requests.

## Requirements

- Python 3.11+
- Docker
- OpenSearch
- Optional OCR dependencies for image processing
- Optional connector-specific dependencies

## Install

```bash
python -m venv .venv
source .venv/bin/activate

pip install -e '.[dev]'
```

With OCR support:

```bash
pip install -e '.[dev,ocr]'
```

Start OpenSearch:

```bash
docker compose up -d
```

Create the index:

```bash
thread-knowledge init-index
```

Run tests:

```bash
pytest
```

## Mock connector

The default public development connector can be used for local development and
retrieval testing without external credentials.

```bash
thread-knowledge index-hn --limit 100
```

Connectors are adapters only. A connector should map its source into the common
thread model:

```text
Thread
 ├─ messages/replies
 ├─ timestamps
 ├─ authors
 ├─ links
 └─ attachments
```

Additional connectors can be added without changing storage, OCR, retrieval,
or MCP layers.

## MCP server

Start the MCP server:

```bash
thread-knowledge-mcp
```

Available tools:

- `search_threads(query, limit)` — find related discussions.
- `get_thread(thread_id)` — load the complete discussion.
- `annotate_thread(thread_id, text, kind)` — persist reusable knowledge about a thread.
- `annotate_item(thread_id, item_id, text, kind)` — attach reusable knowledge to a specific message or attachment.

Annotations should contain verified, reusable information only. Do not persist transient reasoning or unsupported conclusions.

## Production notes

- Run OpenSearch with authentication, TLS, persistent volumes, backups, and restricted network access.
- Keep raw source data so indexes and OCR output can be rebuilt.
- Run ingestion incrementally and avoid reprocessing unchanged items.
- Keep OCR local unless data policy explicitly allows external processing.
- Keep source content separate from AI-derived annotations.
- Monitor ingestion failures, OCR failures, index size, and MCP latency.
- Start with BM25 search. Add local embeddings only if evaluation shows a measurable improvement.
