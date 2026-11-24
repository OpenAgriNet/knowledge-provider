# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Knowledge Provider receives files, pushed JSON, and content fetched from approved URLs. FastAPI exposes intake and management, Temporal runs processing, and PostgreSQL stores documents and revisions. Each network selects a revision independently. Qdrant holds the search projection.

Full narrative docs (read these before deep architectural changes, don't re-derive):
- `README.md` — what the system does, API surface, supported inputs, auth model
- `docs/SEARCH.md` — current search contract, network authorization, and recorded BV query comparison
- `docs/SYSTEM_DESIGN.md` — historical DEV/PROD architecture, retained for migration context
- `docs/architecture-pipeline-rbac.md` — historical login and publication gates
- `docs/MASTER_SCHEME_CATALOG_AND_AI_SYNC_DESIGN.md` — original scheme catalog design (historical: the Postgres master catalog and Redis push it describes were removed; the pull API `/catalog/v1/*` remains)
- `ENV.md` — full annotated environment variable reference (`.env.example` for a working template)

## Commands

```bash
# Run the full stack (api, worker, temporal, minio, qdrant, ui)
docker compose up -d --build
docker compose down
docker compose --profile observability up -d   # + otel-collector & ClickHouse (needs OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318)

# Tests (pytest.ini: testpaths=tests, markers unit/integration/slow/api/db/workflow)
uv run pytest
uv run pytest -m "not slow"                    # skip slow tests
uv run pytest tests/test_activities.py -v      # single file
uv run pytest tests/test_api.py::test_name     # single test

# Lint
uv run ruff check .

# Quick sanity checks (no test infra needed)
uv run python -c "import pipeline.api"
cd ui && npm run build

# UI dev server
cd ui && npm run dev        # vite, talks to API on :8001
cd ui && npm run build

# Health checks against a running stack
curl http://localhost:8001/health
curl http://localhost:6333/                       # qdrant
curl http://localhost:9000/minio/health/live
```

Local ports: UI `3000`, API `8001`, Qdrant `6333`, Temporal `7233`, Temporal UI `8080`, MinIO API `9000`/console `9001`, Keycloak `8082` (path `/auth`).

Interactive API docs once running: `http://localhost:8001/docs`.

## Architecture

### Everything lives in `pipeline/`, not split into microservices

`pipeline/api/` layout:
- `api.py` composes the app and exports runtime dependencies. `__init__.py` re-exports `app`.
- `application.py` constructs FastAPI and its middleware. `runtime.py` owns service startup and shutdown.
- `routers/` holds feature handlers: documents and reviews, upload and crawl, search and catalog, provider and Registry management, authentication, and operations.
- `document_helpers.py`, `file_helpers.py`, `audit_helpers.py`, and `index_helpers.py` hold shared handler helpers.
- `request_logging.py` and `rate_limiting.py` configure request diagnostics and limits.

Rules:
- Add new endpoints to their feature router under `routers/`. Register new routers in `api.py`.
- Update `tests/test_api_routes.py` whenever a route is added, removed or renamed.
- Patch runtime dependencies (for example, `temporal_client`, `object_store`, and startup factories) on `pipeline.api.api`, not the package. Routers and startup read those overrides at call time.
- Shared helper exports remain available on `pipeline.api.api` for existing callers and tests.
- Walk routes with `tests/route_inventory.py`, not `app.routes`: FastAPI hides included routers there.

Core packages under `pipeline/`

- `temporal/workflows/` owns workflow definitions and retry policies. `temporal/activities/` owns the I/O activities. Run `python -m pipeline.worker` to start the worker on `ocr-pipeline`. Preserve registered workflow names, activity names, and patch markers so saved histories can replay.
- `db/` owns PostgreSQL queries. Its public interface remains `pipeline.db`. Add queries to the module that owns the affected record. Documents and jobs return application-shaped records keyed by workflow ID or document UUID. See [the schema guide](docs/POSTGRES_SCHEMA.md).
- `documents/` owns content models, format admission, dates, text cleanup and the document repository. `intake/` receives pushed content, fetches URLs and runs scheduled pulls. `metadata/` owns schema packs, language variants and metadata normalization.
- `network/` owns connection configuration, admission and release policy. Its `adapters/` package owns protocol dispatch for Beckn and other implementations. `network/catalog/` owns catalog projection and its deployment profile.
- `search/` owns passage projection, projection identity and query expansion. `vector_store/` owns Qdrant transport and embedding providers.
- `telemetry/logs.py` exports structured logs to OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set.
- `ocr/`, `translation/`, `chunking/`, `object_storage/` and `vector_store/` contain implementation interfaces and factories. `auth/` validates Keycloak tokens, resolves instance membership, and administers identities.

### Provider pattern

`ocr/`, `translation/`, `chunking/`, `vector_store/` each define a `base.py` protocol and one-or-more concrete implementations selected by env var (e.g. `OCR_PROVIDER`, `TRANSLATION_PROVIDER`, `CHUNKING_PROVIDER`) via a `service.py` factory. Adding a new provider means implementing the base protocol and registering it in that package's service dispatcher — without changing Temporal activity call sites.

### Temporal workflow model

`DocumentPipelineWorkflow` pauses at review stages via Temporal **signals** and resumes on operator action (the approve-* API endpoints send the signal). Stage order (`DocumentStage` in `documents/models.py` / `PIPELINE_STAGES`):

```
registered → ocr_processing → ocr_review → translation_processing → translation_review
  → chunking → chunk_review → ready_for_ingestion → ingesting
  → approval_for_prod → ingesting_prod → completed   (or → failed at any point)
```

`approval_for_prod`/`ingesting_prod` are **PROD-only stages** (`PROD_ONLY_STAGES` in `documents/models.py`); with `DISABLE_PROD_SETTING=true` a document goes straight from `ingesting` to `completed`. That flag is read only at workflow *start* and passed in as an argument — never read inside a running workflow, since that would break Temporal replay determinism if flipped mid-flight (see the docstring in `network/release_policy.py`).

### Revisions and networks

PostgreSQL freezes document content in a revision. Release staging holds destination details. Each document and network has its own release pointer. A managed publication activates that pointer after indexing and a matching network receipt. A legacy approval path remains for deployments that use the older publishing settings.

### Storage responsibilities (don't blur these)

- **Postgres** — canonical metadata/content/review state (documents, workflows, pages, chunks, artifacts, audit, search settings). Source of truth for anything editable.
- **MinIO** — blob storage for original uploads, normalized files, and stage artifacts (OCR/translation/chunk exports, vector-index payload snapshots).
- **Temporal** — orchestration/retries/review-gate signaling only; not a content store.
- **Qdrant** — downstream search projections of approved chunk state; never the place to edit content.

### Authentication

Keycloak authentication is required by default. `AUTH_DISABLED=true` is limited to the local runtime profile. Keycloak owns people, service accounts, and provider membership. The console uses the configured Keycloak browser flow with PKCE. Use [Keycloak passwordless setup](docs/KEYCLOAK_PASSWORDLESS.md) for console sign-in.

### UI (`ui/`)

React 18 + Vite + Tailwind v4 + Radix UI operator console (not a plain upload form) — dashboard, document operations (stage/artifacts/jobs/pages/chunks/vector-index state/audit in one view), search workbench, settings, audit log. `ui/src/` layout: `views/`, `components/` (+ `components/ui/` for primitives), `hooks/`, `lib/`, `auth/`, `config/`, `styles/`.

### Scripts (`scripts/`)

Operational tools include `list_failed_workflows.py`, `terminate_stuck_workflows.py`, `keycloak_bootstrap_docs_pipeline.py`, `backfill_provider_id.py`, and `export_search_contract.py`. Run `python -m scripts.export_search_contract` to regenerate the published request schema. The archived migration kit stays beside the backup. Check existing tools before adding another.
