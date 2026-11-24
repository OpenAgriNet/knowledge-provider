# Document Ingestion Pipeline

[![codecov](https://codecov.io/gh/OpenAgriNet/knowledge-provider/graph/badge.svg?token=2TKM2CR2AC)](https://codecov.io/gh/OpenAgriNet/knowledge-provider)

Knowledge Provider turns files, pushed JSON and approved URLs into searchable documents. Temporal runs processing, PostgreSQL stores the document and revision history, object storage keeps originals and artifacts, and Qdrant serves search.

The sample environment enables `KP_INDEX_ONLY=true`. New documents run through extraction, optional translation, chunking and indexing without approval. Upload, submissions and search use the same `VECTOR_DB_COLLECTION_NAME`. This mode does not release documents to a network.

The system is intentionally operational, not just algorithmic. Documents move through explicit stages, every major output can be persisted as an artifact, and the operator UI is designed to inspect and manage the pipeline rather than hide it.

> **Architecture & design:** this README covers *what the system does and how to run it*.  
> - **[docs/SYSTEM_DESIGN.md](docs/SYSTEM_DESIGN.md)** — system design + diagrams (request/data flow, containers)  
> - **[docs/architecture-pipeline-rbac.md](docs/architecture-pipeline-rbac.md)** — login, roles, state lane vs Super Admin PROD gate

## What This System Does

At a high level, the pipeline supports:

- ingestion of document files, images, office documents, and spreadsheets
- normalization into a canonical processing form
- OCR and extraction
- optional translation for non-English content
- chunk generation
- manual review and correction of pages, translations, and chunks
- indexing of searchable chunks into Qdrant
- operational inspection of workflow, artifacts, audit history, and index state

The system is suitable for:

- knowledge base ingestion
- multilingual document processing
- regulated or review-heavy ingestion workflows
- search and retrieval pipelines that need provenance and operator controls

## Core Architecture

The platform is composed of six main services:

- `api`
  - FastAPI application exposing ingestion, review, artifact, search, and admin endpoints
- `worker`
  - Temporal worker running OCR, translation, chunking, ingestion, and state-update activities
- `temporal`
  - workflow orchestration and retry engine
- `minio`
  - object storage for original uploads, normalized files, and stage artifacts
- `qdrant`
  - vector and lexical search index for approved chunks
- `ui`
  - React operator console for dashboard, document review, search workbench, settings, and audit

```text
                           +------------------+
                           |  Operator UI     |
                           |  React console   |
                           +---------+--------+
                                     |
                                     v
+------------------+        +--------+---------+        +------------------+
| Source Documents | -----> | API / Control    | <----> | Temporal         |
| PDFs, images,    |        | Plane            |        | orchestration    |
| office, sheets   |        | FastAPI          |        | and retries      |
+--------+---------+        +---+----------+---+        +--------+---------+
         |                      |          |                      |
         |                      |          |                      |
         v                      v          v                      v
+--------+---------+   +--------+--+   +---+---------------+   +------------------+
| MinIO             |   | Postgres  |   | Worker            |   | Lang Detect      |
| originals,        |   | canonical |   | normalize, OCR,   |   | language hints   |
| normalized files, |   | metadata, |   | translate, chunk, |   | before           |
| artifacts         |   | pages,    |   | ingest            |   | translation      |
+------------------+   | chunks     |   +---+---------------+   +------------------+
                       +-----+------+       |
                             |              |
                             +--------------+
                                            |
                                            v
                                   +--------+---------+
                                   | Qdrant           |
                                   | search index     |
                                   | approved chunks  |
                                   +------------------+
```

## Conceptual Pipeline Stages

The pipeline is stage-based. Each stage has a purpose, a persistent state transition, and a corresponding operator surface.

### 1. Registered

The document has been accepted into the system and assigned a workflow identifier.

Typical outputs:

- document row in Postgres
- original file reference
- initial job record

### 2. OCR Processing

The source file is normalized if needed and passed through OCR or native structured extraction.

Typical behavior:

- PDFs and office/image inputs are normalized toward a document-processing form
- CSV and XLSX inputs can be parsed without OCR
- OCR output is produced page by page conceptually, then persisted into the document state

### 3. OCR Review

Operators inspect extracted page content and correct OCR mistakes before downstream processing continues.

This is where the system becomes review-driven instead of fully automatic.

### 4. Translation Processing

Non-English content is translated into a target language for downstream chunking and search.

The current implementation keeps translation provider and model metadata so translation outputs remain attributable.

### 5. Translation Review

Operators review machine translation before chunking. This is important in multilingual or domain-heavy corpora where terminology needs supervision.

### 6. Chunking

Reviewed page content is transformed into chunks suitable for search and retrieval.

Each chunk is expected to remain traceable to:

- document
- page start
- page end
- chunk order
- run configuration

### 7. Chunk Review

Operators can inspect, edit, exclude, and eventually tag chunks before ingestion.

This is also the right stage for future chunk tagging and reindex-dirty tracking.

### 8. Ready For Ingestion

Final gate before indexing approved chunks.

### 9. Ingesting

Approved chunks are written into Qdrant using a passage-style schema.

### 10. Completed

The document is fully processed and indexed.

### 11. Failed

The workflow encountered a non-recoverable failure or exceeded retry limits.

## Data Model

The system is organized around a few core entity types.

### Documents

Top-level business objects representing an ingested source.

### Jobs

Discrete runs such as ingestion, OCR-only, translation-only, chunking, or reingestion operations.

### Artifacts

Persisted files or exports associated with a document and stage, such as:

- original uploads
- normalized files
- OCR JSON exports
- translation JSON exports
- chunk exports
- vector-index payload exports

### Pages

OCR output and page-level review state.

### Chunks

Chunked text, review state, exclusion state, and page-span lineage.

### Index Status

Document-level view of what has been pushed to the vector index.

## Storage Responsibilities

### Postgres

Postgres is the canonical metadata and review-state store. Its schema is in `migrations/` and described in `docs/POSTGRES_SCHEMA.md`; the api and worker apply pending migrations at startup. The migration rehearsal kit lives beside the legacy backup under `bh-prod-data/migrations/` in the OpenAgriNet workspace.

It owns:

- document rows
- workflow rows (one per processing run, shown as jobs)
- page rows
- chunk rows
- artifact metadata
- audit logs
- search settings
- document/index status
- inbound webhook records (`webhook_records`, written by `POST /webhook`, `x-api-key` = `WEBHOOK_API_KEY`)

### MinIO

MinIO stores document and stage artifacts.

Typical artifact types include:

- original uploads
- normalized PDF or spreadsheet outputs
- OCR page exports
- translation exports
- chunk exports
- vector-index payload snapshots

### Temporal

Temporal is the orchestration layer.

It is responsible for:

- retries
- workflow lifecycle
- review gates
- long-running task resilience

It is not the canonical store for edited content.

### Qdrant

Qdrant is the search-facing index.

It should be treated as a downstream projection of approved chunk state, not as the source of truth for content editing.

## Supported Inputs

The ingestion layer accepts these source types:

- PDF documents
- text-heavy image assets:
  - `.png`
  - `.jpg`
  - `.jpeg`
  - `.webp`
  - `.tif`
  - `.tiff`
- images:
  - scanned pages, posters, forms, and photo captures are treated as OCR candidates
- office documents:
  - `.doc`
  - `.docx`
  - `.ppt`
  - `.pptx`
  - `.xls`
  - `.xlsx`
- delimited and spreadsheet data:
  - `.csv`
  - `.xlsx`

Normalization behavior by class:

- document-centric inputs
  - PDF, image, Word, and PowerPoint inputs are normalized toward a PDF-like document-processing form before OCR
- spreadsheet-centric inputs
  - CSV, XLS, and XLSX inputs can remain structured tabular artifacts and may use native extraction instead of OCR when that produces better outputs
- multilingual inputs
  - OCR output can continue into language detection and translation before chunking

In practice this means the pipeline can ingest:

- scanned reports
- born-digital PDFs
- presentation decks
- office handbooks and manuals
- spreadsheets and rate sheets
- image-only notices and circulars

General behavior:

- document-like inputs are normalized toward PDF processing
- spreadsheet inputs can remain spreadsheet-oriented and skip OCR when native parsing is better

## Repository Layout

```text
pipeline/
  api/           FastAPI routes and application startup
  auth/          Keycloak authentication and instance permissions
  documents/     Document models, format handling and validity
  intake/        Webhooks, mapping, URL fetching and scheduled intake
  metadata/      Knowledge metadata, language and schema packs
  db/            PostgreSQL queries organized by the records they own
  network/       Publication, admission, Registry and catalog adapters
  search/        Passage projection, index identity and query expansion
  temporal/      Workflows, registered activities and worker startup
  telemetry/     OpenTelemetry logging
  object_storage/, ocr/, translation/, chunking/, vector_store/
                 Storage and processing implementations
ui/              React operator console
scripts/         Operational and maintenance scripts
docs/            Supporting design and operational notes
tests/           Automated tests
test_data/       Small local fixtures for tests and smoke checks
docker-compose.yml
Dockerfile
pyproject.toml
```

## Services And Ports

Default local ports from `docker-compose.yml`:

- UI: `3000`
- API: `8001`
- Qdrant: `6333`
- Temporal: `7233`
- Temporal UI: `8080`
- MinIO API: `9000`
- MinIO console: `9001`
- Keycloak: `8082` (relative path `/auth`; only used when auth is enabled)

## Run locally

Use `docker compose up -d --build` with the credentials and service settings in [ENV.md](ENV.md). The standard setup uses embedded PDF text and local embeddings. Add an OCR provider for scanned documents.

For an API and worker connected to existing services, use the [short settings starter](.env.example). For the older single-stack Compose deployment, keep its deployment settings and consult [ENV.md](ENV.md).

### Logs in ClickHouse (optional)

Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318` in `.env`, then:

```bash
docker compose --profile observability up -d --build
curl 'http://127.0.0.1:18123/?user=knowledge&password=knowledge' \
  --data-binary 'SELECT ServiceName, Body FROM otel.otel_logs ORDER BY Timestamp DESC LIMIT 20'
```

API and worker lines land in `otel.otel_logs`. Shared environments send to the ClickStack collector instead; see `ENV.md` and `docs/ADR/0007-ship-logs-over-otlp.md`.

### Health Checks

Useful endpoints after startup:

```bash
curl http://localhost:8001/health
curl http://localhost:6333/
curl http://localhost:9000/minio/health/live
```

## Environment Variables

Important runtime variables include:

- `MISTRAL_API_KEY`
- `TEMPORAL_HOST`
- `VECTOR_DB_URL`
- `OBJECT_STORE_ENDPOINT` and `OBJECT_STORE_BUCKET`
- `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` for static S3 credentials
- `DATABASE_URL`
- `TRANSLATION_PROVIDER`
- `TRANSLATION_MODEL`
- `TRANSLATION_PAGE_CONCURRENCY`
- `TRANSLATION_MAX_RETRIES`
- `TRANSLATION_RETRY_BASE_SECONDS`
- `TEMPORAL_MAX_CONCURRENT_ACTIVITIES`
- `DOCUMENT_METADATA_CSV_PATH`
- `DOCUMENT_DESCRIPTIONS_JSONL_PATH`
- `CORS_ORIGINS`

Authentication is enabled by default. Configure an issuer and audience for the API.

- `AUTH_DISABLED=true` requires `KP_RUNTIME_PROFILE=local` and is reserved for isolated local tests.
- `KEYCLOAK_ISSUER`
- `KEYCLOAK_JWKS_URL`
- `KEYCLOAK_AUDIENCE` (default `docs-pipeline-api`)
- `DEFAULT_INSTANCE`
- `KEYCLOAK_ISSUER`, `KEYCLOAK_AUDIENCE`, `KEYCLOAK_CLIENT_ID`, and `KEYCLOAK_CLIENT_SECRET`
- UI (Vite, build/runtime): `VITE_AUTH_ENABLED` (default `false`), `VITE_KEYCLOAK_URL`, `VITE_KEYCLOAK_REALM`, `VITE_KEYCLOAK_CLIENT_ID` (default `docs-pipeline-ui`)

[`.env.example`](.env.example) is the short starter for API and worker processes connected to existing services. [ENV.md](ENV.md) lists optional settings.

Optional metadata files:

- `DOCUMENT_METADATA_CSV_PATH`
- `DOCUMENT_DESCRIPTIONS_JSONL_PATH`
  - optional per-document descriptions enrichment file

If these files are not present, the pipeline still works; metadata enrichment is simply reduced.

## Hosting Model

The simplest production-style deployment pattern is:

- expose the UI at a public hostname
- expose the API either:
  - behind the same domain under `/api`, or
  - at a separate internal hostname behind a reverse proxy
- keep Temporal, MinIO, and Qdrant internal to the deployment network

Recommended routing shape:

- `https://your-ui-host/` -> UI
- `https://your-ui-host/api/` -> API

This keeps browser calls same-origin and avoids hardcoded environment-specific domains in the frontend.

## Authentication (Keycloak)

Authentication is enabled by default. Only the isolated local profile can bypass login for disposable tests.

The API validates the Keycloak issuer, signature, and audience. Provider membership comes from the configured Keycloak group root, such as `/providers/{CODE}/contributor`. Deployments with legacy group paths can pin their existing root. Realm roles and `instances` claims remain compatibility inputs.

Product roles map to API permissions, and group-derived (or claim) instances scope access per tenant:

| Realm role | Permissions |
|---|---|
| `superadmin` / `master_admin` | everything (instance-unrestricted, platform-wide) |
| `admin` | state-level: upload, review, pipeline, search (scoped by `instances` claim) |
| `content_curator` | upload, review, pipeline, search |
| `viewer` | search |

The default Compose stack exposes Keycloak at `http://localhost:8081/auth`. It shares the PostgreSQL server with the pipeline and Temporal and uses its own database and role. Set up your realm before opening the console. [Database and migration settings](ENV.md#shared-postgresql-in-docker)

### Configure passwordless sign-in

The console redirects to Keycloak. Keycloak authenticates the user with a passkey or an organization's passwordless identity provider, then returns a PKCE authorization code to the console. The API exchanges that code using the confidential client and validates the returned access tokens on protected routes. Follow the [Keycloak setup and transition guide](docs/KEYCLOAK_PASSWORDLESS.md) to configure the client, enroll users, and verify the callback.

## Operator UI

The UI is an operations console, not just an upload form.

Current views include:

- dashboard
- new document
- document operations
- search workbench
- settings
- audit log

The document operations screen is intended to expose:

- current stage
- stage runtime
- artifacts
- jobs
- pages
- translations
- chunks
- vector-index state
- audit history

## API Overview

The API is organized around a few major groups.

### Ingestion

- `POST /documents` processes an object already registered to the provider.
- `POST /upload` accepts an optional `display_name` as the source title. The filename supplies the default title. Send descriptions and localized labels in `knowledge_metadata`.
- `POST /upload/batch` accepts multiple files and returns a result for each.
- `PATCH /documents/{workflow_id}/scheme-metadata` — also accepts `display_name`; `409` once the document has been published to Dev.

### Document Listing And Summary

- `GET /documents`
- `GET /documents/summary`
- `GET /documents/{workflow_id}`
- `GET /documents/{workflow_id}/runtime`
- `GET /documents/{workflow_id}/jobs`
- `GET /documents/{workflow_id}/stage-io`

### Page Review

- `GET /documents/{workflow_id}/pages`
- `PATCH /documents/{workflow_id}/pages/{page_number}`
  Send `translation_language` with `edited_translation`, `translation_reviewed`, or `translation_notes` to update one existing language. Omitting the language uses the page's default translation. The audit entry records the affected language.
- `POST /documents/{workflow_id}/pages/{page_number}/reset`

### Translation Review

Translation data is surfaced through the page model and review endpoints.

### Chunk Review

- `GET /documents/{workflow_id}/chunks`
- `PATCH /documents/{workflow_id}/chunks/{chunk_number}`
- `POST /documents/{workflow_id}/chunks/{chunk_number}/reset`

### Approval Gates

- `POST /documents/{workflow_id}/approve-ocr`
- `POST /documents/{workflow_id}/approve-translation`
- `POST /documents/{workflow_id}/approve-chunks`
- `POST /documents/{workflow_id}/approve-ingestion`

### Artifacts

- `GET /documents/{workflow_id}/artifacts`
- `GET /documents/{workflow_id}/artifacts/{artifact_id}`
- `GET /documents/{workflow_id}/artifacts/{artifact_id}/content`

### Index And Search

- `GET /documents/{workflow_id}/qdrant`
- `GET /documents/{workflow_id}/qdrant/chunks`
- `POST /documents/{workflow_id}/reingest`
- `POST /search` validates retrieval controls and filters before candidate selection. `document_kind` and `document_code` select an exact document identity across eligible documents. `group` narrows the caller's existing instance access. Network requests require an authenticated principal grant and `network_id`; the server resolves the collection and returns only the selected, current, enabled release. Local operator requests may use `valid_on` or `include_expired` for inspection. See [search contract and BV examples](docs/SEARCH.md).
- `GET /indexes/summary`
- `GET /indexes/{index_name}/settings`
- `GET /indexes/{index_name}/stats`

### Audit And Settings

- audit endpoints
- search runtime settings endpoints

The easiest way to inspect the complete surface is to run the API and open the generated OpenAPI docs at:

```text
http://localhost:8001/docs
```

## Search Model

The search workbench and API support a configurable retrieval surface, including:

- hybrid, tensor, or lexical modes
- candidate pool sizing
- final result limits
- hybrid alpha
- RRF tuning
- optional query expansion profile
- optional E5 query prefixing
- rerank mode selection
- per-document result diversity

The default example index name in this showcase branch is:

- `documents-index`

## Scripts

The `scripts/` directory contains operational helpers for:

- listing failed workflows
- terminating stuck workflows
- backing up persistent volumes
- Keycloak bootstrap and role maintenance

These are intended as operator tools, not hidden one-off commands.

## Tests

Run the test suite with:

```bash
pytest
```

Useful quick checks:

```bash
python3 -m compileall -q pipeline
cd ui && npm run build
```

## Design

See **[docs/SYSTEM_DESIGN.md](docs/SYSTEM_DESIGN.md)** for system design
(containers, data stores, auth, DEV/PROD) and the box-and-arrow diagrams:
[`system-design-diagram.png`](docs/system-design-diagram.png),
[`architecture-diagram.png`](docs/architecture-diagram.png).  
For role-based access and Super Admin prod gate, see
**[docs/architecture-pipeline-rbac.md](docs/architecture-pipeline-rbac.md)**.

## Design Notes

This repository favors explicitness over silent automation:

- review stages are visible
- artifacts are first-class
- runtime state is inspectable
- downstream indexing is a separate concern from content ownership

That makes it a good fit for teams that need document traceability and operational control rather than a black-box ingestion flow.
