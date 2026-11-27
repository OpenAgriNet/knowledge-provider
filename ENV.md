# Environment variables — Frontend & Backend

Single reference for every environment variable used by the docs-pipeline **UI (FE)** and **API / worker (BE)**.  
Use [the short starter](.env.example) when API and worker processes connect to existing services. This page is the reference for optional settings.

---

## Frontend (Vite / `ui/`)

Anything the browser needs **must** be prefixed with `VITE_`.  
Load from `ui/.env` for local development or supply build-time settings through Compose.

| Variable | Default | Required when | Purpose |
|----------|---------|---------------|---------|
| `VITE_API_PROXY_TARGET` | `http://api:8001` (compose) / use `http://localhost:8001` locally | Dev server | Vite proxy target for `/api` → FastAPI |
| `VITE_AUTH_ENABLED` | `false` | — | `true` shows the login page and requires SSO; `false` opens the app without login |

### Frontend auth notes

- The console sends email and code requests to the Python API. The API talks to Keycloak privately. [Email sign-in setup](docs/KEYCLOAK_PASSWORDLESS.md)
- When auth is enabled, the UI calls `GET /api/auth/me` with `Authorization: Bearer <token>`.  
  Backend must have `AUTH_DISABLED=false` and matching `KEYCLOAK_*` or the API cannot validate the session.

### Recommended production FE values

```bash
VITE_AUTH_ENABLED=true
VITE_API_PROXY_TARGET=http://localhost:8001   # or in-cluster API URL in compose
```

---

## Backend (API + worker)

Root `.env` (see `.env.example`). Required by FastAPI (`pipeline/api/`), Temporal worker (`pipeline/worker.py`), and activities.

### Document kinds

Set `DOCUMENT_KINDS` to a comma-separated list of kind codes such as `advisory,scheme,field-report`. Add a schema mapping for every custom kind using `DOCUMENT_KIND_SCHEMAS`, for example `{"field-report":"example:FieldReport"}`. API and worker startup reject a kind without a mapping. Scheme and advisory default to `openagrinet:KnowledgeAdvisory`; video defaults to `openagrinet:VideoResource`. Kinds partition catalogs independently, so several kinds can share one schema. Network publication also requires a projection for the kind in the selected adapter.

Set `INTAKE_SUBJECT_SCHEMA` in the API service to the path of an approved local JSON or YAML schema. Mount the file read-only. The console reads the `subjectCategories.items.enum` declaration from the root schema or a component in an OpenAPI file and offers those values on file and URL intake. If you leave the setting unset, subject categories remain optional free-text labels.

Set `INTAKE_METADATA_PROFILES` in the API and worker services to the path of the [optional metadata profile file](test_data/intake-metadata-profiles.example.yaml). Mount the file read-only and restart API and worker replicas together after changes. A profile selects a document kind and optionally one subject category. One matching category profile takes precedence over the kind default. If multiple selected categories each have a profile, intake asks the user to choose one of them. Other categories and free-text tags may still accompany it.

The console submits the selected profile ID, version and digest as `knowledge_metadata.attribute_profile`, with values in `knowledge_metadata.attributes`. The backend resolves and validates the profile for all new intake, including webhook and URL submissions that omit the pin. A stale pin returns a validation error. Use self-contained object schemas. Fields can be typed or have enumerated values. The profile's `additionalProperties` controls free key/value attributes. Postgres stores the pin with the accepted values. Keep old profile files with deployment history when exact validation replay is needed. Registry-installed network schema packs remain independent. Existing documents and exact webhook retries keep their saved profile.

### Object storage

The API and worker use the same settings. Keep both on the same bucket and backend so existing document and artifact references continue to resolve.

| Variable | Default | Purpose |
|----------|---------|---------|
| `OBJECT_STORE_TYPE` | Unset | Set to `s3` for AWS or an S3-compatible service such as MinIO, or to `gcs` for Google Cloud Storage. Setting any `OBJECT_STORE_*` variable selects the new configuration group. |
| `OBJECT_STORE_ENDPOINT` | `https://s3.amazonaws.com` | HTTP or HTTPS origin. Use an explicit origin for a local S3-compatible server. |
| `OBJECT_STORE_BUCKET` | Required | Bucket for pipeline artifacts. `OBJECT_STORE_CREATE_BUCKET=true` opts into creation at startup. |
| `OBJECT_STORE_CREDENTIALS_TYPE` | `static` for S3, `google_adc` for GCS | S3 accepts `static` or `aws_web_identity`. Web identity requires HTTPS and the role ARN and projected token file. The adapter accepts the standard injected AWS variables. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | Unset | Standard static S3 pair and optional session token. A complete explicit `OBJECT_STORE_*` credential group, including mounted `_FILE` values, takes precedence. |
| `OBJECT_STORE_REGION` | Unset | S3 client and AWS token exchange region. For GCS, an optional location used only when creating a bucket. |

#### Google Cloud Storage

Set these values on both the API and worker and remove the S3 endpoint override.

```dotenv
OBJECT_STORE_TYPE=gcs
OBJECT_STORE_BUCKET=knowledge-documents-example
OBJECT_STORE_CREDENTIALS_TYPE=google_adc
GOOGLE_CLOUD_PROJECT=example-project
OBJECT_STORE_CREATE_BUCKET=false
```

The Google SDK uses [Application Default Credentials](https://docs.cloud.google.com/docs/authentication/application-default-credentials). Attach a service account or configure Workload Identity Federation for production. If using a credential configuration file, mount it read-only and set `GOOGLE_APPLICATION_CREDENTIALS` to the mounted path on both processes. Omit `GOOGLE_CLOUD_PROJECT` if ADC can resolve the project. Provide the workload identity with object read, write, overwrite, and delete access to the bucket.

Provision the bucket before startup. If explicitly enabling creation with `OBJECT_STORE_CREATE_BUCKET=true`, set `OBJECT_STORE_REGION=asia-south1` or another GCS location and grant the identity permission to create and inspect the bucket. Stored references use `blob://bucket/key`; when switching backends, copy existing objects to the same named buckets and keys, or migrate the stored references in Postgres.

The following legacy variables apply when the new object store group is absent.

| Variable | Default | Purpose |
|---|---|---|
| `BLOB_STORAGE_PROVIDER` | `minio` | `minio` for existing deployments or `s3` for an S3-compatible service. |
| `BLOB_STORAGE_ENDPOINT` | MinIO or AWS default | Host and optional port. MinIO uses `MINIO_ENDPOINT` or `localhost:9000`. S3 uses `s3.amazonaws.com`. |
| `BLOB_STORAGE_BUCKET` | MinIO uses `MINIO_BUCKET` or `documents` | Required for S3. Provision the bucket before starting when auto-creation is off. |
| `BLOB_STORAGE_TLS` | Off for MinIO, on for S3 | Whether to use HTTPS. |
| `BLOB_STORAGE_CREATE_BUCKET` | On for MinIO, off for S3 | Check for and create the bucket at API startup. |
| `BLOB_STORAGE_CREDENTIALS` | `static` | Use `aws_web_identity` with S3 over TLS to exchange an AWS service account token for temporary credentials. |
| `BLOB_STORAGE_ACCESS_KEY` and `BLOB_STORAGE_SECRET_KEY` | Backend-specific keys | Set both to override `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY` for MinIO, or `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` for S3. `BLOB_STORAGE_SESSION_TOKEN` can accompany the explicit pair. |
| `BLOB_STORAGE_REGION` | S3 uses `AWS_REGION` when present | Optional region for the S3 bucket and the AWS token exchange. |
| `AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE` | Required for `aws_web_identity` | The IAM role and projected token file supplied by the workload runtime. |

### Core infrastructure

| Variable | Default | Purpose |
|----------|---------|---------|
| `TEMPORAL_HOST` | `localhost:7233` | Temporal gRPC address |
| `TEMPORAL_MAX_CONCURRENT_ACTIVITIES` | `4` | Worker activity concurrency |
| `LOG_LEVEL` | `INFO` | Worker log verbosity (`DEBUG`/`INFO`/`WARNING`/`ERROR`); the `temporalio` SDK logger stays at `INFO` regardless, to avoid its own noisy debug output |
| `DATABASE_URL` | See [database configuration](#shared-postgresql-in-docker) | Knowledge Provider connection. The API and worker apply the SQL migrations on startup. |
| `DB_POOL_MIN_SIZE` | `1` | Connection pool minimum per process |
| `DB_POOL_MAX_SIZE` | `10` | Connection pool maximum per process (api and worker each hold their own pool) |
| `DB_POOL_TIMEOUT_SECONDS` | `30` | Seconds to wait for a free pooled connection |

### Shared PostgreSQL in Docker

The default Compose stack runs one PostgreSQL 18.7 server. The non-root `postgres-init` job creates `knowledge_provider`, `temporal`, `temporal_visibility`, and `keycloak` on a fresh volume. Each service uses its own role. The Temporal role owns both Temporal databases. Database connect permissions prevent cross-service access. The Temporal schema job runs before the server starts, and the namespace job runs before the pipeline starts.

Set these four passwords in the same `.env` file. Generate a distinct value for each. Optionally set `POSTGRES_USER` (default `kp_db_admin`) before initializing a fresh volume.

| Setting | Used by |
| --- | --- |
| `POSTGRES_PASSWORD` | The server's provisioning role. The old bootstrap name remains a fallback. |
| `DB_PASSWORD` | Knowledge Provider and its SQLite importer |
| `TEMPORAL_DB_PASSWORD` | Temporal server and schema job |
| `KC_DB_PASSWORD` | Keycloak |

The pipeline connects to `postgres:5432/knowledge_provider` with its scoped role. The password is passed separately from the connection URI so it needs no URL encoding. When running the pipeline against an external database, configure `DATABASE_URL` and your PostgreSQL authentication mechanism. For a Compose file at a different path, pass the same file to `--env-file` and set `KP_ENV_FILE` to its absolute path. The deploy script does this when `ENV_FILE` is set.

The bundled Keycloak uses a Docker Hardened Image and stays on the private Compose network. Create or import your realm and configure the console client before enabling user access. When an older realm depends on a custom authenticator, confirm that the target image loads the provider. [Passwordless setup](docs/KEYCLOAK_PASSWORDLESS.md)

The standard Compose file pins the infrastructure images. Container builds use Docker Hardened Images as their base. Pulls from `dhi.io` require an authenticated Docker account with access to the selected tags. If your account cannot pull the MinIO tag, set `KP_MINIO_IMAGE` to a registry image you have approved and verified with a non-root runtime. [Docker Hardened Images setup](https://docs.docker.com/dhi/how-to/use/)

#### Move data from an older stack

1. Take consistent logical backups of each existing database. Record the source image versions, the Temporal cluster's configured history shard count, and the active workflow count. Retain the old volumes and Compose configuration for rollback.
2. Create a new Compose project with a fresh `shared-postgres-dhi` volume and unused ports. Run `docker compose up -d postgres`, then `docker compose run --rm postgres-init`. Restore each database into its corresponding empty target as its new owner. Restore custom-format dumps with `pg_restore --no-owner --no-privileges --exit-on-error --single-transaction`. Transfer objects using the object store API to the new volume.
3. Set `TEMPORAL_HISTORY_SHARDS` to the source cluster's value before starting Temporal from a restored database. Follow the supported server upgrade sequence for the source version. If the migration starts from SQLite without Temporal history, apply the Knowledge Provider schema, rehearse the SQLite import, and start a new Temporal namespace.
4. Verify document and artifact counts, Temporal namespaces and workflows, and Keycloak realm access on the isolated target. Test passwordless login with the actual realm. Switch traffic only after those checks pass.

For a SQLite cutover, use the operator kit under `bh-prod-data/migrations/` in the OpenAgriNet workspace. It verifies the backup, applies the application schema in an isolated project and dry-runs the SQLite import. It also restores object references, the identity database and the historical Qdrant serving set.

The default model cache uses a named Docker volume. To reuse a host cache, set `MODEL_CACHE_MOUNT` to a directory accessible to Docker. If the model files are under a `hub` subdirectory, set `HF_HUB_CACHE` to that directory's path inside the container, such as `/opt/hf-cache/hub`.

### Logs to ClickHouse (OTLP)

Off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set. When it is set, API and worker log lines at `INFO` and above (root logger, including `/search` query text) also go over OTLP/HTTP to a collector, which writes them to ClickHouse for HyperDX. See `docs/ADR/0007-ship-logs-over-otlp.md`.

| Variable | Default | Purpose |
|----------|---------|---------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(unset — off)* | Collector base URL. Local: `http://otel-collector:4318` with `docker compose --profile observability`. Shared env: the ClickStack collector, e.g. `http://clickstack-otel-collector.observability.svc.cluster.local:4318` |
| `OTEL_EXPORTER_OTLP_HEADERS` | *(unset)* | Headers sent with each export, e.g. `authorization=<HyperDX ingestion API key>`; ClickStack rejects unauthenticated telemetry |
| `OTEL_SERVICE_NAME` | `knowledge-provider` | Labels our lines in the shared store. Compose sets `knowledge-provider-api` / `knowledge-provider-worker`; set the same per process in other deployments |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | `5` (seconds) | How long one export may take before it is dropped; kept under Docker's 10s stop grace so shutdown with the collector down stays clean |

### API HTTP surface

| Variable | Default | Purpose |
|----------|---------|---------|
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated browser origins (add `http://localhost:3001` if UI uses that port) |
| `RATE_LIMIT_DEFAULT` | `100/minute` | Default rate limit |
| `RATE_LIMIT_UPLOAD` | `10/minute` | Upload rate limit |
| `KEYCLOAK_PROVIDER_GROUP_ROOT` | `providers` | Parent Keycloak group for provider membership. Set `states` on deployments that retain the legacy group paths. |
| `PORTAL_INSTANCE` | `DEFAULT_INSTANCE` | Optional provider code for the existing platform content and catalog identity. Pin the current code before upgrading an existing deployment. |
| `PROVIDER_DISPLAY_NAMES` | `{}` | JSON object of provider codes to display names. |
| `KNOWLEDGE_TIMEZONE` | `UTC` | IANA timezone used for date-based validity. |
| `QUERY_EXPANSION_RULES` | `{}` | JSON object of named literal expansion profiles. A profile maps query phrases to expansions. |
| `LEGACY_SCHEME_PROFILE_PATH` | Empty | Optional deployment JSON file for legacy catalog aliases, tool routes, and route exceptions. |
| `DOCS_PIPELINE_API_URL` | request base URL | Public API base for provenance/links |
| `DOCS_PIPELINE_UI_URL` | `http://localhost:3000` | Public UI base for links |
| `DEFAULT_INSTANCE` | `default` | Default tenant / instance id |

For example, set the following in `.env` and select the `field-operations` profile in search settings or send `"query_expansion_profile": "field-operations"` to `POST /search/`.

```dotenv
QUERY_EXPANSION_RULES='{"field-operations":{"soil test":"soil analysis nutrient assessment","drip":"micro irrigation"}}'
```

A case-insensitive substring match appends each matching expansion once. The original query remains at the start. The default profile `none` disables expansion, and unknown profile names leave the query unchanged.

### Webhook (`POST /webhook`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `WEBHOOK_MAX_RECORDS` | `500` | Max records per `/webhook` request; more returns 413 |
| `ADVISORY_BACKFILL_ON_START` | `false` | When `true`, the worker queues an index workflow for every unindexed `webhook_records` row once at startup (backfill of existing records). Leave off normally; `POST /webhook/advisory/reindex` does the same on demand. |

Each saved record whose `payload_hash` differs from its `indexed_hash` also starts one Temporal workflow `advisory-<record id>` that writes advisory chunks (`document_kind=advisory`) to the Qdrant collection `VECTOR_DB_COLLECTION_NAME`. Admin endpoints (Keycloak admin): `POST /webhook/advisory/reindex`, `POST /webhook/advisory/promote` (copies to `PROD_VECTOR_DB_COLLECTION_NAME` (on `PROD_VECTOR_DB_URL`)), `GET /webhook/advisory/status`.

Body is a JSON array of objects (max `WEBHOOK_MAX_RECORDS`); stored in the `webhook_records` table, upserted on `(source, external_id)` where `external_id` is `contentID` (else `id`, `slug`, payload hash).

### Auth / Keycloak (backend JWT validation)

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTH_DISABLED` | `false` | Set `true` only with `KP_RUNTIME_PROFILE=local` for an isolated development stack. |
| `KP_RUNTIME_PROFILE` | `production` | Use `local` for a disposable, loopback-only stack that explicitly opts out of authentication. |
| `KEYCLOAK_ISSUER` | `''` | Exact issuer in Keycloak tokens. Use the private realm URL on a fresh deployment. |
| `KEYCLOAK_JWKS_URL` | derived from issuer | JWKS URL for the same realm |
| `KEYCLOAK_AUDIENCE` | Required with authentication | Expected API audience. Configure the Keycloak client mapper to add this audience to user and service tokens. Client roles are read only from the client with this ID. |
| `KEYCLOAK_JWT_LEEWAY_SECONDS` | `30` | Clock-skew leeway for `exp`/`nbf` |
| `KEYCLOAK_VERIFY_LIVE_GROUPS` | `false` | Enables provider membership management and checks current groups on each authenticated request. Requires `AUTH_DISABLED=false`, `KEYCLOAK_AUDIENCE`, and configured Keycloak admin access. |
| `KEYCLOAK_CLIENT_ID` | Unset | Confidential console client ID for exchanging authorization codes and refreshing browser sessions. |
| `KEYCLOAK_CLIENT_SECRET` | Unset | Secret for the console client. Keep it on the API. |
| `KEYCLOAK_BACKCHANNEL_URL` | Keycloak issuer URL | Private realm URL for login and token exchange if it differs from the JWT issuer. |
| `KEYCLOAK_CONSOLE_REDIRECT_URI` | `http://127.0.0.1/knowledge-provider/auth/sso-callback` | Exact redirect registered on the confidential console client. Python consumes it internally. |
| `KEYCLOAK_GOOGLE_CONSOLE_URL` | Unset | Enables Google sign-in through the configured Keycloak broker. Set the full console login URL. See [Google callback setup](docs/KEYCLOAK_PASSWORDLESS.md#keep-google-sign-in). |
| `KEYCLOAK_EMAIL_OTP_JAR` | Unset | Absolute path to the reviewed authenticator JAR mounted into the self-hosted Keycloak service. |
| `KEYCLOAK_ADMIN_CLIENT_ID` | `''` | Realm service account client ID for group and user management. |
| `KEYCLOAK_ADMIN_CLIENT_SECRET` | `''` | Secret for the realm service account. Inject through the deployment's secret configuration. |
| `KEYCLOAK_ADMIN_BASE_URL` | from `KEYCLOAK_ISSUER` | e.g. `https://dev-auth-vistaar.da.gov.in/auth` |
| `KEYCLOAK_ADMIN_REALM` | from issuer | Realm to manage (e.g. `bharat-vistaar`) |
| `KEYCLOAK_ADMIN_TOKEN_REALM` | Managed realm (`master` with an admin login) | Realm that issues the admin service account token. |
| `KEYCLOAK_ADMIN_USERNAME` | `''` | Optional admin login for user and group management. With the password, it takes precedence over the service account and signs in through the `admin-cli` client. |
| `KEYCLOAK_ADMIN_PASSWORD` | `''` | Password for `KEYCLOAK_ADMIN_USERNAME`. Inject through the deployment's secret configuration. |

For provider management, configure the service account in `KEYCLOAK_ADMIN_REALM`. It needs permission to read users and groups, create provider groups, and manage group membership. For Keycloak built-in delegation, grant the realm service account `realm-management/manage-users` for user and group operations and `realm-management/view-realm` to read existing realm roles. Pre-create the application roles during realm setup when the service account has no permission to create roles. Open **Users → Provider access**. A platform administrator can create a provider and appoint an administrator by verified email. A provider administrator can manage member roles within that provider. Revocation takes effect on the member's next API request, including requests with an older token.

**FE ↔ BE pairing (required for SSO):**

| Frontend (`ui/.env`) | Backend (root `.env`) |
|----------------------|------------------------|
| same realm | `KEYCLOAK_JWKS_URL` = `{ISSUER}/protocol/openid-connect/certs` |
| `VITE_AUTH_ENABLED=true` | Send Bearer tokens to the authenticated backend. |

Use the bundled private Keycloak service or an existing realm reachable from the API.

### OCR (Chandra)

OCR is a **separate process** (not in `docker-compose`). The worker calls
`CHANDRA_VLLM_BASE_URL` (HF mode → `POST {base}/ocr/pages`).

| Variable | Default | Purpose |
|----------|---------|---------|
| `DOCUMENT_PARSER_TYPE` | See legacy fallback | Use `pypdf`, `mock`, `liteparse`, `chandra`, `mistral`, or `extension` for a single parser. |
| `DOCUMENT_PARSER_PROFILES` | Unset | Comma-separated profile names. Set `DOCUMENT_PARSER_DEFAULT` and prefix each profile's options with `DOCUMENT_PARSER_<NAME>_`. |
| `DOCUMENT_PARSER_SEGMENT_PAGES` and `DOCUMENT_PARSER_SKIP_TEXT_LAYER` | `20`, `true` | Page batch size and whether to bypass OCR when PDF text is already present. |

Use one of the following parser configurations in the same `.env`. Named profiles use the same settings with a profile name inserted after `DOCUMENT_PARSER_`.

```dotenv
# Embedded PDF text
DOCUMENT_PARSER_TYPE=pypdf

# LiteParse layout extraction
# DOCUMENT_PARSER_TYPE=liteparse
# DOCUMENT_PARSER_EXECUTABLE=lit

# Chandra via a vLLM server
# DOCUMENT_PARSER_TYPE=chandra
# DOCUMENT_PARSER_INFERENCE_MODE=vllm
# DOCUMENT_PARSER_ENDPOINT=http://localhost:8010/v1
# DOCUMENT_PARSER_MODEL=chandra

# Mistral OCR
# DOCUMENT_PARSER_TYPE=mistral
# DOCUMENT_PARSER_MODEL=mistral-ocr-latest
# DOCUMENT_PARSER_API_KEY_FILE=/run/secrets/mistral-api-key

# Deployment-installed parser extension
# DOCUMENT_PARSER_TYPE=extension
# DOCUMENT_PARSER_PLUGIN=my-parser
# DOCUMENT_PARSER_OPTION_MODEL_PATH=/opt/models/my-parser
```

The legacy parser variables apply when the `DOCUMENT_PARSER_*` group is absent.

### Additional file formats

Install the format package on the API and worker. Add its entry point name to the comma-separated `DOCUMENT_FORMAT_EXTENSIONS` setting. The package exports a `pipeline.document_formats.DocumentFormat` instance in the `knowledge_provider.document_formats` entry point group. The definition declares a unique lowercase file suffix, its exact HTTP media type, and a `normalize_to_pdf(input_path, output_path)` function. The worker passes the normalized PDF to the selected document parser. Keep the package installed while documents in that format may need reprocessing.

The upload form discovers the new suffix through the upload options API. The crawler recognizes the declared media type on allowlisted HTTPS sources. File size limits and source policy apply to these formats too.

| Variable | Default | Purpose |
|---|---|---|
| `OCR_PROVIDER` | `chandra` | Default parser provider, including `chandra`, `mistral`, `pypdf`, and `liteparse`. |
| `OCR_ALLOWED_PROVIDERS` | `OCR_PROVIDER` | Comma-separated allowed parser choices for the upload form field `parser_provider`. Install and configure each on the worker. |
| `LITEPARSE_EXECUTABLE` | `lit` | Optional LiteParse CLI for local embedded PDF text and page coordinates. Use an OCR provider for scanned pages. |
| `OCR_MODEL` | `chandra` | Model name |
| `CHANDRA_VLLM_BASE_URL` | `''` | Base URL; local HF default `http://localhost:8010/v1` |
| `CHANDRA_OCR_API_URL` | `''` | Alternate full OCR API URL (overrides base path build) |
| `CHANDRA_INFERENCE_MODE` | `hf` | `hf` = HTTP page API; `vllm` = chandra-ocr client + OpenAI base |
| `CHANDRA_MAX_OUTPUT_TOKENS` | `12288` | Max generation tokens |
| `CHANDRA_OCR_MAX_WORKERS` | `4` | Parallel OCR workers |
| `CHANDRA_IMAGE_DPI` | `192` | Page render DPI |
| `CHANDRA_REQUEST_TIMEOUT_SECONDS` | `300` | Request timeout |
| `OCR_MAX_SPLIT_PAGES` | `40` | Max split pages |
| `OCR_SEGMENT_PAGES` | `20` | Segment size for long docs |
| `CHANDRA_HF_HOME` / `HF_HOME` | — | Hugging Face cache (HF server / scripts) |

**Local start (real model, GPU + torch + model download):**

```bash
# requires: pip install chandra-ocr torch  (and enough VRAM/disk for Chandra weights)
python scripts/chandra_hf_server.py   # listens on :8010
```

**Local unblock without GPU (placeholder OCR):**

```bash
python scripts/mock_chandra_ocr_server.py   # same :8010 API surface as HF server
# or in-process (no HTTP server): OCR_PROVIDER=mock  # restart worker
```

An extension package registers a factory in the Python entry point group `knowledge_provider.document_parsers`. The factory takes a dictionary of strings from the profile's `OPTION_*` settings and returns a `pipeline.ocr.base.OcrProvider`. Package installation and profiles are controlled by the deployment. Upload callers can select only a configured profile name.

### Translation (Gemma)

| Variable | Default | Purpose |
|----------|---------|---------|
| `TRANSLATION_TYPE` | See legacy fallback | Use `none` or `openai_compatible`. |
| `TRANSLATION_BASE_URL` | See legacy fallback | Base URL for an OpenAI-compatible inference server. Set `TRANSLATION_MODEL` to a model served there. |
| `TRANSLATION_PROVIDER` | `gemma_vllm` | Provider. Set `none` to keep source text and supplied variants without generating translations. |
| `TRANSLATION_MODEL` | `gemma-4-31b-it` | Model id |
| `TRANSLATION_VLLM_BASE_URL` | `http://localhost:8020/v1` | OpenAI-compatible endpoint |
| `TRANSLATION_API_KEY` | `''` | Optional API key |
| `TRANSLATION_PAGE_CONCURRENCY` | `1` | Parallel pages |
| `TRANSLATION_MAX_RETRIES` | `6` | Retry count |
| `TRANSLATION_RETRY_BASE_SECONDS` | `2.0` | Backoff base |
| `TRANSLATION_MAX_OUTPUT_TOKENS` | `8000` | Max tokens |
| `TRANSLATION_REQUEST_TIMEOUT_SECONDS` | `300` | Timeout |
| `TRANSLATION_USE_MAX_COMPLETION_TOKENS` | `false` | `true` sends `max_completion_tokens` instead of `max_tokens` — required by some OpenAI-compatible endpoints (e.g. Azure AI Foundry) that reject `max_tokens` outright |
| `TRANSLATION_TEMPERATURE` | `0.0` | Sent as `temperature` in the request. Set to empty (`TRANSLATION_TEMPERATURE=`) to omit the field entirely — some endpoints (e.g. Azure AI Foundry) reject any non-default temperature. Independent of `TRANSLATION_USE_MAX_COMPLETION_TOKENS` |
| `DISABLE_PROD_SETTING` | `false` | `true` skips the `approval_for_prod` and `ingesting_prod` stages — documents complete at DEV ingest. Read at workflow start; in-flight documents keep the shape they began with |
| `TRANSLATION_SCRIPT_GATE_ENABLED` | `true` | Regex script gate: only pages with non-Latin (Indic) script are translated. `false` restores per-line pyfranc detection, which misreads OCR noise as European languages |
| `TRANSLATION_SCRIPT_MIN_CHARS` | `15` | Minimum non-Latin characters on a page before it counts as non-English |
| `TRANSLATION_SCRIPT_MIN_RATIO` | `0.05` | Minimum share of all letters that must be non-Latin |
| `SCRIPT_FAMILIES_CONFIG_PATH` | unset (bundled `pipeline/translation/script_families.json`) | Path to a JSON file that fully replaces the script → language table used by the regex gate and pyfranc disambiguation |

### Domain tagging

| Variable | Default | Purpose |
|----------|---------|---------|
| `DOMAIN_TAGGING_ENABLED` | `true` | Enable/disable |
| `DOMAIN_TAGGING_PROVIDER` | `gemma_vllm` | Provider |
| `DOMAIN_TAGGING_MODEL` | falls back to `TRANSLATION_MODEL` | Model |
| `DOMAIN_TAGGING_VLLM_BASE_URL` | falls back to `TRANSLATION_VLLM_BASE_URL` | Endpoint |
| `DOMAIN_TAGGING_API_KEY` | falls back to `TRANSLATION_API_KEY` | Key |
| `DOMAIN_TAGGING_STRICT_TAXONOMY` | `true` | Restrict tags to taxonomy |
| `DOMAIN_TAXONOMY_PATH` | package default | Path to taxonomy JSON |
| `DOMAIN_TAGGING_CONCURRENCY` | `4` | Parallelism |
| `DOMAIN_TAGGING_REQUEST_TIMEOUT_SECONDS` | `120` | Timeout |
| `DOMAIN_TAGGING_MAX_OUTPUT_TOKENS` | `1024` | Max tokens |

### Knowledge metadata extraction

The document pipeline can propose metadata after it saves the source text and chunks. Set `METADATA_AUTO_EXTRACT=true` to run it automatically: after chunking, each included chunk gets its own suggestion; chunks are sent several to a request (about 12,000 characters, each chunk a numbered block), `METADATA_CONCURRENCY` requests at a time, and the document suggestion is the union of those chunk suggestions with each crop and category listed once. Fields in `METADATA_AUTO_ACCEPT_FIELDS` are accepted on the chunks and the document. A chunk without its own value for a field inherits the document's. A retry skips chunks already tagged, and a failed chunk is logged without stopping the document. **Content → Document metadata → Generate** does the same for an existing document: it tags chunks that have no suggestion yet and combines all chunk suggestions (a document that is not chunked yet is read directly). When a reviewer saves or accepts chunk metadata, a document list built from chunks is rebuilt; a machine-accepted document list follows the chunks, while a document list a reviewer accepted or edited is kept and only its suggestion refreshes. Open a specific chunk and choose **Chunk metadata → Generate metadata** to review that chunk alone. Use **Edit metadata** to set or correct saved values, or **Edit suggestions** to revise a proposal. **Document metadata** also lets reviewers save the inclusive start and end dates. A blank end date means no expiry. Chunks inherit the document validity period. PostgreSQL saves changes immediately. Chunk overrides replace the corresponding document field. Turn off an override to inherit the document value. Re-ingest the document to refresh Qdrant. Each crop and category is suggested once, with the first passage that names it; seasons and locations keep one suggestion per passage because they are paired by the sentence that connects them.

| Variable | Default | Purpose |
| --- | --- | --- |
| `METADATA_BASE_URL` | Unset | An OpenAI-compatible base URL ending in `/v1`. For Ollama on a Docker host, use `http://host.docker.internal:11434/v1`. For OpenAI, use `https://api.openai.com/v1`. |
| `METADATA_MODEL` | Unset | The exact model ID advertised by the endpoint, for example `gemma4:latest` on the local Ollama server. |
| `METADATA_API_KEY` | Unset | Optional bearer credential for this endpoint. |
| `METADATA_AUTO_EXTRACT` | `false` | Generate chunk metadata suggestions after chunking and combine them into the document suggestion. Explicit document and chunk actions still work when the endpoint and model are configured. |
| `METADATA_USE_MAX_COMPLETION_TOKENS` | `false` | Send `max_completion_tokens` instead of `max_tokens` for models that require it. |
| `METADATA_AREA_CODES` | `{}` | JSON that maps source area names or aliases to governed codes. Example `{"Example District":{"codeScheme":"example","areaCode":"D1"}}`. Unmapped names stay visible as evidence and are unavailable for exact area filtering. |
| `METADATA_EXTRACT_FIELDS` | all four | Comma list from `category,crop,season,location` the model is asked for and that are kept. |
| `METADATA_AUTO_ACCEPT_FIELDS` | empty | Fields an automatic run accepts without review (origin `extracted-auto`). Empty keeps every suggestion pending. Human-edited values for the same field are not overwritten. |
| `METADATA_MAX_TOKENS` | `4096` | Output token cap per window. |
| `METADATA_TEMPERATURE` | unset | Sent only when set. |
| `METADATA_REQUEST_TIMEOUT_SECONDS` | `30` | Per-request timeout. |
| `METADATA_TOTAL_TIMEOUT_SECONDS` | `600` | Whole-extraction timeout. It runs inside the 30-minute chunking activity, so keep it well below that. |
| `METADATA_CONCURRENCY` | `4` | Requests in flight at once. A rate limit (429) or outage (5xx) is retried up to six times, waiting as the endpoint's `Retry-After` says (at most 120 s). Lower it if the endpoint rate-limits. |
| `METADATA_CHUNK_TAGGING_TIMEOUT_SECONDS` | `1500` | Total time to tag all chunks of a document. Keep it below the 30-minute chunking activity. On timeout, chunks already tagged stay saved, the rest are reported as failed, and a retry or **Generate** resumes them. A chunk or combined document suggestion with more than 256 facts is not stored whole: a chunk fails, and the document keeps its first 256 and records `facts_total`. |
| `METADATA_PROMPT_PATH` | unset | File that replaces the built-in extraction prompt. |
| `METADATA_MAX_WINDOWS` | unset | Read only the first N windows (about 12,000 characters each) of a long document instead of failing with "Source exceeds the metadata extraction limit". Without it, up to 120 windows (about 300 pages) are read. The proposal records `windows_used` and `windows_total`. Reviewers add anything missed with **Edit metadata**. |

The extractor sends the saved text to the configured endpoint. Use an endpoint authorized for that content. An automatic extraction failure leaves the document processing pipeline able to finish and can be retried in Content. The source is processed in bounded windows. Oversized inputs return an error so a partial proposal cannot look complete.

The opt-in `POST /search/` filter accepts `metadata_filters` with `scope` (`document` or `chunk`), `category`, `crop`, `season` and `area` (an object with `codeScheme` and `areaCode`). Crop and season names match normalized exact labels. For example, `{"scope":"chunk","category":"Crop","crop":"Wheat","season":"Rabi"}` selects chunks whose effective values match Wheat and Rabi. An override takes precedence for its field. If area and season are both present, they must match the same applicability rule.

Qdrant stores the effective category in `subject_categories`. A passage stores values in `document_scope` only when the corresponding document filter values differ. Document extensions are stored in `metadata.attributes`. To attach an extension to a particular subject, supply its `attributes` in `knowledge_metadata.subjects`. For example, a Wheat subject can have `{"sowingDays":14}` while a Rice subject has separate values. Qdrant keeps these under `metadata.subjects`. They are payload data and have no automatic search filters.

### Chunking

| Variable | Default | Purpose |
|----------|---------|---------|
| `CHUNKING_PROVIDER` | `recursive_splitter` | Provider (`recursive_splitter` or `deterministic`) |
| `CHUNKING_MODEL` | provider name | Model id (label only; no LLM calls) |
| `CHUNKING_TARGET_CHUNK_TOKENS` | `450` | Target chunk size |
| `CHUNKING_MAX_CHUNK_TOKENS` | `450` | Max chunk size |
| `CHUNKING_MIN_CHUNK_TOKENS` | `100` | Min chunk size |
| `CHUNKING_OVERLAP_TOKENS` | `128` | Overlap |
| `CHUNKING_MAX_PAGES_PER_CHUNK` | `8` | Max page span |
| `CHUNKING_PAGE_WINDOW_SIZE` | `8` | Window size |
| `CHUNKING_FALLBACK_PROVIDER` | `deterministic` | Fallback provider |
| `CHUNKING_REQUEST_TIMEOUT_SECONDS` | `120` | Timeout |

### Document titles and descriptions

The document name is the primary display title. Set a description and localized labels in `knowledge_metadata` on file upload or in a knowledge submission. Keys under `localized` are language tags. Search chooses the title and description for the language of the returned passage when those labels are available.

```json
{
  "description": "Instructions for the public notice",
  "localized": {
    "fr-CA": {"title": "Avis public", "description": "Instructions pour cet avis"}
  }
}
```

The SQLite importer carries titles, descriptions, and tags from manifest rows linked to a single document. It uses the stored manifest association first and falls back to the source filename. It reports ambiguous or unmatched rows. For metadata supplied in a separate legacy file, map its columns into `knowledge_metadata` before reingesting the document.

### Vector backend (Qdrant)

Search, index status, deletes, and ingestion go through `pipeline/vector_store` → Qdrant.

| Variable | Purpose |
|----------|---------|
| `KP_INDEX_ONLY` | Set to `true` on the API and worker to index new intake automatically in the configured collection and disable publishing. The sample .env enables it. |
| `VECTOR_DB_URL` | Vector DB base URL (e.g. `http://localhost:6333` or reverse-proxy HTTPS) |
| `VECTOR_DB_API_KEY` | Vector DB API key (required for non-local hosts) |
| `VECTOR_DB_COLLECTION_NAME` | Collection name (default `documents-index`) |
| `VECTOR_DB_TIMEOUT_SECONDS` | Client timeout |
| `EMBEDDING_PROVIDER` | `sentence_transformers` (local) or `openai_compatible` |
| `EMBEDDING_MODEL` | Embedding model id (default `intfloat/multilingual-e5-large`) |
| `EMBEDDING_VECTOR_SIZE` | Vector dimensions (default `1024`) |
| `EMBEDDING_PROMPT_STYLE` | `e5` (default), `embeddinggemma`, or `plain`. A model or prompt change requires a fresh collection and a full reindex. |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` | Remote embeddings API when `openai_compatible` |

### Master Scheme Catalog (AI tool / prompt sync)

Exposes `/catalog/v1/*` so **bharat-oan-api** and **bharat-provider-backend** can refresh scheme lists and tool prompts without redeploying hard-coded registries.

| Variable | Default | Purpose |
|----------|---------|---------|
| `CATALOG_SERVICE_API_KEYS` | Empty | Optional comma-separated catalog service keys. Authenticated callers using a key must also be allowed by the provider scope. |
| `CATALOG_SERVICE_PROVIDER_SCOPE` | Empty | Comma-separated provider codes allowed for the optional catalog service keys. |
| `CENTRAL_INSTANCES` | `default` | Instances that default `network_visible=true` for new scheme docs |
| `PROD_VECTOR_DB_URL` | — | PROD vector DB for document promote |
| `PROD_VECTOR_DB_API_KEY` | — | PROD vector DB key |
| `PROD_VECTOR_DB_COLLECTION_NAME` | `documents-index` | PROD collection for normal documents |
| `PROD_SCHEME_VECTOR_DB_URL` | `PROD_VECTOR_DB_URL` | PROD vector DB for scheme promotes |
| `PROD_SCHEME_VECTOR_DB_API_KEY` | `PROD_VECTOR_DB_API_KEY` | Scheme collection API key |
| `PROD_SCHEME_VECTOR_DB_COLLECTION_NAME` | `schemes-index` | Must **not** equal documents collection |

**Endpoints:** `GET /catalog/v1/snapshot`, `/version`, `/schemes`, `/tool-prompt`; `POST /catalog/v1/rebuild`; `PATCH /documents/{id}/scheme-metadata` (body: `kind`, `code`, `name`, `aliases`, `tool_routing`, `catalog_visible`, `network_visible`, `valid_from`, `valid_to`). The catalog is computed from documents (one row per document), so there is no bootstrap endpoint. `/upload`, `/documents` and `/upload/batch` take a required `kind` (`scheme` or `advisory`) and an optional `instance` and `code`.

### Publish to Network

`SEARCH_NETWORK_PRINCIPALS` is a JSON object mapping verified Keycloak subject IDs to permitted serving network IDs, for example `{"adapter-service-subject":["oan.dev"]}`. The default is `{}`, granting no network retrieval access. Use the subject of the provisioned adapter service account, not its display name or a value supplied in a request. Requests from a listed principal must specify `network_id`, even when its grant list is empty. The local authentication bypass grants no network retrieval access. The API resolves the collection from its managed targets and still applies instance restrictions. Configure this value on the API; the worker does not use caller retrieval grants. The adapter remains responsible for verifying the incoming participant and NxN route.

A listed principal can call only retrieval, scoped catalog, its own identity and read-only search settings endpoints. It cannot read document, chunk, revision, export or audit routes through an unrelated permission.

Network requests cannot use `include_expired`, historical `valid_on`, or raw diagnostic hits. Local operator inspection retains those controls. `/search/collections` lists only the caller's permitted network targets. See [Search contract and BV examples](docs/SEARCH.md).

A network release selects a specific document revision. Automatic release follows the source policy and configured destination. An authorized provider operator can also select a saved revision for each managed network. The worker stages its search passages and records the publisher's receipt before activating the revision for that destination.

For Beckn, the worker pins the prepared catalog, endpoint, sender, receiver, audience, and schema selection to the publication transaction. The announced provider must match the provider staged with the revision. A retry can reuse acceptance only with the same publication policy. If the policy changed or an older transaction has no policy digest, reconcile the earlier delivery and select the revision in a new release transaction.

For Beckn, `BECKN_CATALOG_PROFILE_PATH` points to a JSON file keyed by document kind. It contains the catalog and resource identifiers, names, and attributes for each published capability. The bundled [profile](pipeline/network/catalog/profiles/beckn-catalogs.json) supplies defaults. Mount overrides on the API and worker, then restart them. Keep the wire identifiers when editing descriptions for an existing capability.

The default Beckn provider identity combines the local provider code and destination network ID. Use `PROVIDER_CONFIG_OVERRIDE_PATH` when an existing provider has a different registered ID. Mount that file on the API and worker. See the [example](deploy/network_group_providers.example.json).

A named connection selects a network ID, an adapter, and its release collection. Set a default connection for automatic releases. A connection with a release collection can also receive a revision selected by an authorized provider operator. Give each network its own collection. The older Discovery settings apply when `NETWORK_CONNECTIONS` is absent.

Set `AUTO_RELEASE_NETWORK_ID` to enable automatic release on the default connection. The connection supplies the release collection and any separate Beckn scheme collection. If a deployment still sets `AUTO_RELEASE_COLLECTION` or `AUTO_RELEASE_SCHEME_COLLECTION` explicitly, each value must agree with its managed collection.

```dotenv
NETWORK_CONNECTIONS=dev
NETWORK_CONNECTION_DEFAULT=dev
NETWORK_CONNECTION_DEV_TYPE=beckn
NETWORK_CONNECTION_DEV_ID=oan.dev
NETWORK_CONNECTION_DEV_DISCOVERY_URL=http://discovery-adapter:8080
NETWORK_CONNECTION_DEV_SENDER_ID=provider.oan.dev
NETWORK_CONNECTION_DEV_RECEIVER_ID=discovery.oan.dev
NETWORK_CONNECTION_DEV_VISIBLE_TO=oan.dev
# NETWORK_CONNECTION_DEV_RELEASE_COLLECTION=dev-published
# NETWORK_CONNECTION_DEV_SCHEME_RELEASE_COLLECTION=dev-schemes
# Optional Registry lookup for this network
# NETWORK_CONNECTION_DEV_REGISTRY_URL=http://registry:8081
# NETWORK_CONNECTION_DEV_REGISTRY_TOKEN_FILE=/run/secrets/registry-token
# NETWORK_CONNECTION_DEV_APPROVED_ROUTES_FILE=/etc/knowledge-provider/dev-routes.json
```

An approved routes file opts a network into Registry checks before managed OnDemand publication. The file is a JSON array. Each entry specifies `network_id`, `participant_id`, `capability`, `action`, `base_url`, `method`, `path`, `mapping_url`, `mapping_sha256`, `mapping_runtime`, and `auth_profile`. Optional `timeout_ms` and `retry_max` default to 15000 and 0. Use the exact approved `select` binding and a pinned catalog schema pack. The worker checks the Registry's active participant and action against the file and records the evidence digest with the receipt. The network adapter must verify and install the mapping and authentication configuration separately.

#### Install another network adapter

Install the adapter package in the API and worker images. Its factory must be registered in the `knowledge_provider.network_adapters` entry point group under the configured `TYPE`. The factory receives the named `NetworkConnection`. It implements the [adapter contract](pipeline/network/adapters/contracts.py) for supported kinds, provider identity, publication admission, and delivery.

```dotenv
NETWORK_CONNECTIONS=notices
NETWORK_CONNECTION_DEFAULT=notices
NETWORK_CONNECTION_NOTICES_TYPE=my-network-adapter
NETWORK_CONNECTION_NOTICES_ID=notices.example
NETWORK_CONNECTION_NOTICES_RELEASE_COLLECTION=published-notices
NETWORK_CONNECTION_NOTICES_OPTION_ENDPOINT=https://notices.example/publish
NETWORK_CONNECTION_NOTICES_OPTION_TOKEN_FILE=/run/secrets/network-token
```

Each `OPTION_` name becomes a lowercase option key for that adapter. Option names and authentication are defined by the installed package. Use lowercase provider IDs so that search can apply its existing provider filter. The adapter receives the staged revision and stable transaction ID. It must verify the remote receipt and map successful delivery to `result_status="ACCEPTED"` and `context_matches=True`. Return a stable admission evidence digest when the adapter verifies a route or policy. The core stores the receipt, compares admission evidence on retry, and activates the release after acceptance.

An adapter can implement `prepare_delivery(request)` to prepare its outbound publication and return a SHA-256 digest of the nonsecret policy used for that message. The core stores the digest with the transaction and checks it before reusing a receipt.

Run [the adapter integration test](tests/test_network_adapter_extensions.py) against an isolated PostgreSQL test database to verify installation, revision selection, receipt recovery, and search visibility with the supplied fixture.


| Variable | Default | Purpose |
|----------|---------|---------|
| `ENABLE_PUBLISH_TO_NETWORK` | `false` | `true` publishes each promoted document's catalog to the network. Anything else skips the step: the document completes and its Publish to Network step shows as skipped. Read when the step runs, so a change applies to the next publish after the worker restarts |
| `DISCOVERY_SERVICE_ENDPOINT` | *(empty — required)* | Base URL of the external Discovery Service; the pipeline POSTs to `{DISCOVERY_SERVICE_ENDPOINT}/publish` |
| `NETWORK_SENDER_ID` | *(empty — required)* | This instance's Beckn `senderId`, also sent as `networkId` |
| `PROVIDER_CONFIG_OVERRIDE_PATH` | unset (no overrides) | Path to a JSON object of instance code → provider id, set per deployment, e.g. `{"ka": "ka.example.com"}`. Codes are case-insensitive. A missing file, invalid JSON, or an empty or invalid id (same rule as the template) stop the API and worker from starting |
| `NETWORK_PROVIDER_ID_TEMPLATE` | `{instance}.{network_id}` | Default provider id and descriptor code, unless overridden in the file above. `{instance}` is the group code, or `PORTAL_INSTANCE` for documents with no Group; `{network_id}` is `NETWORK_SENDER_ID`. With no `NETWORK_SENDER_ID`, chunks get no `provider_id`. Must use both and render a valid provider id (lowercase letters, digits, `-` and `.`, no leading, trailing or repeated `.`), or the API and worker refuse to start. A document whose instance code renders an invalid id fails its publish without retry |
| `NETWORK_ADVISORY_CATALOG_NAME` | `Advisories from documents` | Name of the advisory catalog |
| `NETWORK_SCHEMES_CATALOG_NAME` | `Schemes from documents` | Name of the scheme catalog |
| `NETWORK_GROUP_CATALOG_NAME_TEMPLATE` | `{group_name} {catalog_name}` | Catalog name for a Group, e.g. `Maharashtra Advisories from documents`. Must use `{group_name}`, or the API and worker refuse to start |
| `DISCOVERY_SERVICE_PUBLISH_INITIAL_INTERVAL_SECONDS` | `30` | Wait before the first retry of a failed publish |
| `DISCOVERY_SERVICE_PUBLISH_BACKOFF_COEFFICIENT` | `2.0` | Multiplier applied to the wait after each retry |
| `DISCOVERY_SERVICE_PUBLISH_MAX_INTERVAL_SECONDS` | `300` | Cap on the wait between retries |
| `DISCOVERY_SERVICE_PUBLISH_MAX_ATTEMPTS` | `5` | Total attempts (including the first) before the stage fails |

---

## Who consumes what

| Concern | FE | API | Worker |
|---------|----|-----|--------|
| `VITE_*` proxies & Keycloak browser login | ✅ | — | — |
| JWT validation (`AUTH_*`, `KEYCLOAK_*`) | — | ✅ | — |
| Temporal / MinIO / Postgres (`DATABASE_URL`) | — | ✅ | ✅ |
| OCR / translation / chunking / domain tags | — | config / status | ✅ runs jobs |
| Qdrant (`VECTOR_DB_*`) | — | ✅ | ✅ |
| Publish to Network (`DISCOVERY_SERVICE_ENDPOINT`, `NETWORK_SENDER_ID`) | — | — | ✅ runs the publish activity |
| Network provider config (`PROVIDER_CONFIG_OVERRIDE_PATH`, `NETWORK_*_PROVIDER_*`) | — | validated at boot | ✅ validated at boot, used by the publish activity |

---

## Local non-Docker quick start

**Backend** (repo root):

```bash
set -a && source .env && set +a
export DATABASE_URL="postgresql://knowledge_provider@localhost:5433/knowledge_provider"
export PGPASSWORD="$DB_PASSWORD"
export CORS_ORIGINS="http://localhost:3000,http://localhost:3001"
export PYTHONPATH="$(pwd)"
.venv/bin/uvicorn pipeline.api:app --host 0.0.0.0 --port 8001 --reload
```

**Frontend** (`ui/`):

```bash
# ui/.env should set VITE_API_PROXY_TARGET=http://localhost:8001
npm run dev -- --port 3001
```

With auth:

```bash
# ui/.env
VITE_AUTH_ENABLED=true

# root .env (API) — same realm as FE
AUTH_DISABLED=true
KP_RUNTIME_PROFILE=local
KEYCLOAK_ISSUER=https://auth-vistaar.da.gov.in/auth/realms/bharat-vistaar
KEYCLOAK_JWKS_URL=https://auth-vistaar.da.gov.in/auth/realms/bharat-vistaar/protocol/openid-connect/certs
KEYCLOAK_AUDIENCE=
```


---

## Files to keep in sync

| File | Role |
|------|------|
| `ENV.md` | This document (human-readable contract) |
| `.env.example` | Short API and worker starter for external services |
| `.env` | Local backend secrets (gitignored) |
| `ui/.env` | Local frontend overrides (gitignored if configured) |
| `docker-compose.yml` | Compose-injected env for API, worker, UI |

## Knowledge intake

Choose the intake route that matches how you receive the content. Each route creates document workflows that produce pages and searchable passages.

| Input | Route | How it becomes a document |
|---|---|---|
| Uploaded file or batch | `POST /upload` or `POST /upload/batch` | The format registry selects a reader or converter for each file. |
| Submitted URL | `POST /providers/{provider}/crawl/{source_id}` | The source profile controls the allowed host and selects a parser or a JSON template. |
| Pushed JSON | `POST /knowledge/intake/{provider}/{source}/submissions` | The source policy accepts canonical JSON or maps the sender's shape to the canonical schema. |

`POST /knowledge/intake/{provider}/{source}/submissions` accepts one canonical knowledge document or an array of up to 250. Send a provider-scoped service token and an `Idempotency-Key` header. Authenticated callers can get the JSON Schema from `GET /knowledge/intake/schema`. Each document includes an `external_id`, `title`, and the full `text`. Optional fields include `kind`, `language`, `translations`, `source_url`, and `knowledge_metadata`.

Translations use language tags as keys. Each value can be a text string or an object with `text` and optional `title` and `description`. Translated labels are stored in `knowledge_metadata.localized`. The source language remains the document's declared `language`.

```json
{
  "external_id": "notice-42",
  "title": "Public notice",
  "text": "Full original notice text.",
  "language": "en",
  "translations": {
    "fr-CA": {"text": "Texte intégral de l’avis.", "title": "Avis public"},
    "es": "Texto completo del aviso."
  }
}
```

The full translations stay on the document page. Search can return a translation when a passage covers the corresponding source page. Smaller passages cite the page, where the full translation is available.

Create or update a source policy with `PUT /knowledge/intake/{provider}/{source}`. Supply a sample payload in `fixture`. Omit both `template` and `mapping` when the sender already uses the canonical schema. For other payloads, provide a JSON Pointer mapping or select a template from `KNOWLEDGE_INTAKE_TEMPLATES_FILE`. The file maps template names to mapping objects. Saving a policy validates the fixture and pins the resolved mapping in that policy version. JSONata is also supported: use `{"runtime":"jsonata-v1","expression":"…"}` as the mapping. Return a canonical document or array. The evaluator runs in a bounded subprocess with no deployment environment or host bindings, and the same canonical validation applies.

With `auto_release: true`, set `release_network_id` on the source policy to publish to a configured network. Leave it blank to use the deployment default. The adapter must support every kind in the mapped sample and each later submission. The worker pins the destination and collection on each new job. Repeating a successful delivery key returns its original job and destination even after a policy edit.

In the provider console, open **Knowledge intake sources** and load or create a policy. Choose a payload format, paste a representative submission, then pin the policy and copy its submission URL. The earlier webhook URL remains available for existing senders.

```json
{
  "notice-feed": {
    "runtime": "json-pointer-v1",
    "records": "/notices",
    "fields": {
      "external_id": "/id",
      "title": "/headline",
      "text": "/body"
    }
  }
}
```

URL intake accepts HTML, plain text, Markdown, canonical JSON, and supported PDF, Office, spreadsheet, CSV, and image media types. A crawl source profile may set `intake_template` for JSON feeds or `parser_profile` for downloaded files. The URL must meet the existing provider host policy. Downloads are limited to 2 MiB for structured content and 20 MiB for files.

For a JSON crawl, PostgreSQL keeps each source record alongside the document produced by its mapping. Repeating the same record and mapped document reuses the receipt. A change to an unmapped source field creates a new receipt and starts a revision for the same document. If a repeat requests a different release network or collection, the API returns `409`. Select the existing revision for the intended network. On replay of an older receipt that stored only mapped content, the service records the first observed source record separately and preserves the original receipt.

File uploads and mixed batches accept the extensions listed by the upload options API. Plain text, Markdown, HTML, and canonical JSON files have a 2 MiB limit. Upload one canonical knowledge document per JSON file. The JSON document's kind, when present, must match the upload kind. HTML uploads are parsed for visible text and their originals download as attachments.

### Scheduled URL intake

Set `SCHEDULED_PULL_PROFILES_FILE` on the worker to a local JSON file. Each profile is scoped to a provider and source. The worker polls due profiles and uses the same bounded HTTPS fetch and canonical intake checks as manual URL intake. A JSON feed can provide an opaque next cursor at a configured JSON Pointer. The worker sends it back as a query parameter to the same URL.

```json
[
  {
    "provider": "example",
    "source_key": "notices",
    "host": "notices.example.org",
    "url": "https://notices.example.org/feed",
    "interval_seconds": 900,
    "next_cursor_pointer": "/next",
    "cursor_parameter": "cursor",
    "mapping": {
      "records": "/items",
      "fields": {
        "external_id": "/id",
        "title": "/headline",
        "text": "/body"
      }
    }
  }
]
```

An optional `authorization_env` names a deployment secret beginning with `CRAWL_AUTH_`. Use `auto_release` only when the provider's publication policy allows it. For JSON feeds, set either `mapping` or `intake_template`. The template name refers to the shared `KNOWLEDGE_INTAKE_TEMPLATES_FILE`. With neither setting, the feed must use the canonical submission schema. Scheduled profiles process JSON, HTML, plain text, Markdown, and the supported binary formats. An optional `parser_profile` selects the parser for a scheduled file.

With `auto_release: true`, a profile can set `release_network_id` to a configured network whose adapter supports the document kind. Omit the field to use the deployment's automatic release destination. The worker records the selected network and collection with the job. A named destination must still use that collection when the worker indexes and publishes the revision. A scheduled poll keeps the original receipt for an unchanged item imported under an earlier policy and continues with new and changed items. The provider console reports how many items on the last committed page kept their earlier policy. A manual crawl returns a conflict for that item. To release already-imported content to another network, select its revision there.

PostgreSQL records the cursor, fetch time, checksum, and retry state. A cursor advances in the same transaction as the page's intake receipts. Failed fetches retain their cursor and retry with backoff. Each profile's settings and mapping are hashed, so an edited profile starts a new checkpoint without changing earlier receipts. The worker indexes drafts in `VECTOR_DB_COLLECTION_NAME`. Automatic release uses the destination network's collection.

To show scheduled sources in the provider console, mount the same profiles file on the API and worker. If a profile names a template, mount the same templates on both services. Only the worker needs the source authorization environment variables. In **Providers**, select a provider and choose **Check status** under **Scheduled intake**. The console shows the current profile, its release policy, and the status at the time of the check. For paginated feeds, **Last page** means the most recent page whose receipts committed. The same view is available to provider administrators through `GET /providers/{provider}/scheduled-sources`.

### Registry operator proposals

A provider manager can submit a local proposal at `POST /providers/{provider}/networks/{network_id}/registry-proposals`. Include a stable UUID `request_id`, the stream's `expected_version`, the proposed `change`, and a `reason`. Start with version zero. When changing an existing Registry record, include its observed record ID and revision. Route proposals require a schema pack installed for the destination network.

Members of the live Keycloak group `/network-operators/<network-id>` can list proposals at `GET /networks/{network_id}/registry-proposals` and record a decision at `PUT /networks/{network_id}/registry-proposals/{proposal_id}/decision`. Decisions require the expected proposal version and a reason. The queue shows pending proposals by default. Set `pending_only=false` to include decision history.

Approval records the operator's decision in PostgreSQL. Sunbird writes and adapter configuration are separate operations. A proposal or approval does not change publication admission or route readiness.
