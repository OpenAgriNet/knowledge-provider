# Knowledge Provider PostgreSQL schema

PostgreSQL stores provider ownership, documents, processing runs, and network releases. A provider owns each document. The combination of provider, kind, and code identifies it within that provider. Each network independently selects a frozen document revision. The legacy catalog API derives catalog membership from the provider and kind. Documents store source and working index metadata as JSONB, and pages store translations as JSONB.

At startup, the API and worker call `pipeline/migrate.py` to create the active schema or apply later numbered migrations. Use the migration kit beside the legacy backup to import SQLite data or apply archived pre-release upgrades.

## Conventions

- Entity tables generally use UUID primary keys. Join and checkpoint tables can use keys formed from their owning records.
- A foreign key is named `<referenced table, singular>_id` (for example `document_id`). A role prefix is added when the role matters (`uploaded_by_user_id`, `started_by_user_id`).
- Text identifiers other systems rely on (`documents.code`, `temporal_workflow_id`) are ordinary columns, not keys.
- `created_at` and `updated_at` are the last two columns of a table. `updated_at` is maintained by one shared trigger. Append-only or short-lived tables skip `updated_at`, as noted per table.
- Timestamps are `TIMESTAMPTZ` (UTC), booleans are `BOOLEAN`, JSON is `JSONB`, lists of strings are `TEXT[]`.
- JSONB columns have a `CHECK` that the value is an object (or array). Value rules inside them live in Pydantic models, not in the database.
- There are no database enums. `pipeline/documents/models.py` validates the plain `TEXT` value sets for stages, workflow types and statuses, and audit entity types. Open sets such as `artifact_type` and `action_type` also use `TEXT`.
- Counts have `CHECK >= 0`. Slugs and identifiers have pattern `CHECK`s.

### Shared trigger

```sql
CREATE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$ LANGUAGE plpgsql;
-- one per table that has updated_at:
-- CREATE TRIGGER trg_<table>_updated_at BEFORE UPDATE ON <table>
--   FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

### Value sets (plain `TEXT`, validated in code)

| Column(s) | Python type (`pipeline/documents/models.py`) | Values |
|---|---|---|
| `workflows.current_stage`, `workflow_artifacts.stage` | `DocumentStage` | `registered`, `ocr_processing`, `ocr_review`, `translation_processing`, `translation_review`, `chunking`, `chunk_review`, `ready_for_ingestion`, `ingesting`, `publishing_to_network`, `approval_for_prod`, `ingesting_prod`, `completed`, `failed` |
| `workflows.workflow_type` | `WorkflowType` | `pipeline`, `ocr_only`, `ocr_retry`, `translation_retry`, `chunking_retry`, `reingest`, `promote_prod` |
| `workflows.status` | `WorkflowStatus` | `running`, `waiting_review`, `completed`, `failed` |
| `audit_logs.entity_type` | `AuditEntityType` | `document`, `page`, `chunk`, `setting`, `catalog` |

The database does not check these values, so adding one needs no migration. Raw SQL that writes them must use a value from the table.

## Tables

The grouped baseline creates 25 application tables plus its migration ledger. Keycloak and Temporal use their own databases on the same PostgreSQL server. Startup applies [the shared extension and trigger](../migrations/bootstrap/00_shared.sql) followed by the domain files in the order shown below. The migration kit owns `legacy_import_evidence` and creates it only when it imports old data. `webhook_records` and `email_otps` are absent from a clean install.

| Bootstrap file | Tables | Purpose |
|---|---|---|
| [Instances and access](../migrations/bootstrap/10_instances_access.sql) | `instances`, `users`, `email_sign_in_limits` | Ownership, actor attribution and email send cooldown. Keycloak owns credentials and group membership. |
| [Documents](../migrations/bootstrap/20_documents_processing.sql) | `documents`, `pages`, `chunks`, `document_revisions` | Editable content and frozen snapshots. |
| [Workflows](../migrations/bootstrap/25_workflows.sql) | `workflows`, `workflow_artifacts`, `document_current_workflow` | Processing history, object references and the revision-to-workflow foreign key. |
| [Intake and source evidence](../migrations/bootstrap/30_intake_source_evidence.sql) | `provider_source_settings`, `webhook_sources`, `webhook_mapping_versions`, `webhook_deliveries`, `source_intake_receipts`, `scheduled_source_pulls` | Provider settings, pinned mappings, delivery and document evidence, and restartable cursors. |
| [Search, audit and catalog](../migrations/bootstrap/40_search_audit_catalog.sql) | `audit_logs`, `search_settings`, `catalogs` | Action history, shared search configuration and catalog identifiers used by document reads and catalog routes. |
| [Network and Registry](../migrations/bootstrap/50_network_registry.sql) | `network_release_staging`, `network_releases`, `publication_deliveries`, `schema_pack_versions`, `registry_operator_subjects`, `registry_operator_proposals`, `registry_operator_decisions` | Release state, delivery receipts, schema bundles and proposal decisions. |
| Migration ledger | `schema_migrations` | Baseline and later applied migrations. |

The views `catalog_status`, `document_current_workflow`, and `legacy_document_catalog_types` store no rows of their own.

Document ownership and processing relationships

```
instances 1 ── * users            (users.instance_id, nullable)
instances 1 ── * documents 1 ── * workflows 1 ── * workflow_artifacts
                                   │
                                   ├── * pages
                                   └── * chunks
instances 1 ── * audit_logs       (audit_logs.instance_id, snapshot, SET NULL)
instances 1 ── * catalogs         (legacy compatibility)
```

Release staging, source receipts, and scheduled cursors survive worker restarts. A source receipt holds replay evidence. The worker commits `last_page_policy_conflicts` with the scheduled page cursor and receipts.

---

## 1. `instances`

One row per tenant node (a state, the centre, or the platform), mirrored from Keycloak. An instance is a provider: it owns catalogs. Rows are created on demand from a tenant code (`get_or_create_instance`) or from a Keycloak group path (`upsert_instance`).

Unique: `keycloak_group_id`, `path`.

| Column | Type | Constraints / Default | Purpose |
|---|---|---|---|
| id | UUID | PRIMARY KEY, DEFAULT gen_random_uuid() | Internal instance ID. |
| keycloak_group_id | TEXT | UNIQUE | Keycloak group ID. NULL until a sync that sees the path fills it (rows made by the SQLite move or by an upload have none). |
| name | TEXT | NOT NULL | Tenant code, lowercase (`mh`, `bv`). This is what the API calls the document's `instance`. |
| path | TEXT | NOT NULL UNIQUE | Keycloak group path: `/states/MH`, or `/global` for `bv`. |
| created_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | Created. |
| updated_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | Last change (trigger). |

Notes
- Only the tenant node is mirrored, not the role leaves (`/states/MH/admin`). Roles and permissions are read from the token, not from this table.
- Role leaves stay in Keycloak; `catalogs.instance_id` points at the tenant node.

---

## 2. `users`

Uploader, reviewer and actor identity, looked up once from the Keycloak token.

Unique: `keycloak_user_id`; unique index on `lower(email)` where `email IS NOT NULL`.

| Column | Type | Constraints / Default | Purpose |
|---|---|---|---|
| id | UUID | PRIMARY KEY, DEFAULT gen_random_uuid() | Internal user ID. |
| keycloak_user_id | TEXT | NOT NULL UNIQUE | Keycloak `sub` claim. `local-admin` when auth is disabled (not a UUID, so it cannot be the PK). |
| username | TEXT | | Current username. |
| email | TEXT | | Current email. |
| roles | TEXT[] | NOT NULL DEFAULT '{}' | Current roles. |
| instance_id | UUID | FK → `instances(id)` ON DELETE SET NULL | The user's instance (tenant). Single membership for now. |
| first_seen_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | First login or upload. |
| last_seen_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | Most recent activity. |

Notes
- Upserted on each authenticated upload or login. A `local-admin` row is seeded by the migration.
- A token can carry several tenants but `instance_id` holds one: `upsert_user(..., tenants=[...])` uses the first code in sorted order and logs a warning when there are several. Authorization still reads the token.
- If users later need several instances, add a `user_instances` join table and drop `instance_id`.
- Point-in-time identity is kept in `audit_logs.actor`.

---

## 3. `catalogs`

One row per provider instance and resource type. A catalog is what a provider publishes on the network: Mahavistar's `scheme` catalog and its `advisory` catalog are two rows. A new type adds rows, not tables.

Unique: `(instance_id, type)`.

| Column | Type | Constraints / Default | Purpose |
|---|---|---|---|
| id | UUID | PRIMARY KEY, DEFAULT gen_random_uuid() | Catalog ID. |
| instance_id | UUID | NOT NULL, FK → `instances(id)` ON DELETE RESTRICT | The provider that owns the catalog. |
| type | TEXT | NOT NULL, CHECK `^[a-z0-9]+:[A-Za-z0-9]+$` | Network schema/capability, e.g. `openagrinet:AgricultureResource`. |
| name | TEXT | NOT NULL | Catalog display name. Defaults to `<instance name> <kind>` when created on upload. |
| metadata | JSONB | NOT NULL DEFAULT '{}', CHECK object | Beckn publish information. See keys below. |
| created_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | Created. |
| updated_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | Last change (trigger). |

### `metadata` keys

| Key | Type | Purpose |
|---|---|---|
| publish_directives | object | Beckn `publishDirectives`. |
| visible_networks | string[] | Networks the catalog is visible to. |
| criteria | object | Catalog criteria. |
| network | object `{catalog_id, last_published_at}` | Set once the catalog is published on the network. |

Pydantic (`CatalogMetadata`) validates the keys; the database only checks for an object.

Notes
- `type` stores the schema name. `pipeline/network/network_constants.py` defines `catalog_type_for_kind` and `kind_for_catalog_type`. The mappings are `scheme` to `openagrinet:AgricultureResource`, `advisory` to `openagrinet:KnowledgeAdvisory`, and `video` to `openagrinet:VideoResource`. See ADR 0003 and ADR 0006.
- Every instance gets one catalog per supported type when the instance row is created (`upsert_instance` calls `ensure_instance_catalogs` in the same transaction). Startup (`init_db`) runs `sync_catalogs_for_all_instances`, so a type added to `KIND_TO_CATALOG_TYPE` reaches existing instances. Upload keeps `get_or_create_catalog` as a fallback for a catalog that is missing. Names are `<INSTANCE> <kind> catalog`, for example `MH scheme catalog`.
- Computed catalog values (status, chunk count, network visibility, `promoted_at`) come from the `catalog_status` view, not stored here.

---

## 4. `documents`

One row per document, classified by its own kind and owned by a provider.

Unique: `(provider_id, kind, code)`.

| Column | Type | Constraints / Default | Purpose |
|---|---|---|---|
| id | UUID | PRIMARY KEY, DEFAULT gen_random_uuid() | Document ID. |
| provider_id | UUID | NOT NULL, FK → `instances(id)` ON DELETE RESTRICT | Provider that owns the document. |
| kind | TEXT | NOT NULL, nonblank | Document classification. Legacy catalog IDs are derived for compatibility. |
| code | TEXT | NOT NULL, CHECK `^[a-z0-9]+(-[a-z0-9]+)*$` | Short code. Unique within its provider and kind. |
| name | TEXT | NOT NULL | Display name. |
| aliases | TEXT[] | NOT NULL DEFAULT '{}', CHECK no empty string | Ordered alternate names used to match queries. The first one is the prompt hint. |
| source | JSONB | NOT NULL, CHECK object, CHECK `source->>'fingerprint' ~ '^[0-9a-f]{32}$'` | The uploaded file. See shape below. |
| is_disabled | BOOLEAN | NOT NULL DEFAULT FALSE | Soft delete. Restore flips it back. |
| metadata | JSONB | NOT NULL, DEFAULT see below, CHECK object | Period, uploader, index state and flags. See keys below. |
| created_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | Row created. |
| updated_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | Last change (trigger). |

Indexes include `(created_at DESC)`, `(provider_id, (source->>'fingerprint'))`, and the unique key `(provider_id, kind, code)`.

### `source`

Facts about the uploaded file, not about processing.

```json
{
  "fingerprint": "<md5 of file contents>",
  "original_filename": "pmkisan.pdf",
  "storage_uri": "s3://...",
  "source_type": "document",
  "canonical_input_type": "pdf"
}
```

| Key | Type | Notes |
|---|---|---|
| fingerprint | string | MD5, 32 lowercase hex. Enforced by CHECK. Also the key the document has inside the vector index. |
| original_filename | string | Name as uploaded. |
| storage_uri | string | Location of the uploaded file. |
| source_type | string | `spreadsheet`, `document` or `unknown`. |
| canonical_input_type | string | `spreadsheet` or `pdf`. Normalized pipeline path. |

There is no page count here: it is the number of `pages` rows.

### `metadata`

| Key | Type | Default | Purpose |
|---|---|---|---|
| valid_from | string `YYYY-MM-DD` | absent | Start of the period chunks are searchable. |
| valid_to | string `YYYY-MM-DD` | absent | End of the period. Absent means no end. Pydantic requires `valid_from <= valid_to`. |
| uploaded_by_user_id | string (users.id) | absent | Who uploaded. Not an FK, so a deleted user leaves a stale id; `audit_logs.actor_user_id` keeps real history. |
| index_status | object | `{}` | Vector index state per index. Keys `dev` and `prod`. See below. |
| catalog_visible | boolean | true | Counts toward the catalog. |
| network_visible | boolean | true | May be published to the Beckn network. |
| reindex_required | boolean | false | Approved content changed and the vector index is stale. |
| reindex_reason | string | absent | Why a reindex is needed. |
| chunking | object `{run_number, provider, model, config, created_at}` | absent | Settings of the run that produced the current chunks. |

Column default: `'{"index_status": {}, "catalog_visible": true, "network_visible": true, "reindex_required": false}'`.

`index_status` is one entry per vector index (DEV and PROD are separate Qdrant collections):

```json
{
  "dev":  {"status": "indexed", "chunk_count_indexed": 42,
           "last_indexed_at": "2026-10-19T06:20:00Z", "last_verified_at": "2026-10-19T06:25:00Z",
           "schema_version": "passage-v1", "details": {}},
  "prod": {"...": "..."}
}
```

- A missing key means the document was never ingested to that index. `status` is `unknown`, `indexed` or `missing`.
- Update one index atomically with `jsonb_set(metadata, '{index_status,dev}', $1::jsonb)`; the `index_status` key always exists, which `jsonb_set` needs.

### Notes
- A document's stage is the `current_stage` of its latest workflow. List and filter queries join the `document_current_workflow` view.
- The API's `instance` comes from `documents.provider_id → instances.name`. Migration 0007 backfills this reference. Migration 0008 backfills the kind and removes the catalog foreign key.
- The document's kind is stored in `documents.kind`. The legacy catalog view maps known kinds to their catalog types. `scheme_code`, `scheme_name` and `scheme_aliases` are `code`, `name` and `aliases`.
- `chunk_count` and `page_count` are counted from `chunks` and `pages`; they are not stored.
- A new document needs a `document_kind` and a `code`. A document's kind is fixed after upload. `DuplicateDocumentCode` is raised when that provider already uses the code for that kind.
- Re-uploading an existing code updates the same document: a new workflow starts, `source` is replaced, pages and chunks are cleared and `reindex_required` is set (`reset_document_for_reupload`). Earlier workflows and artifacts stay as history.
- SQL does not check types inside the JSONB columns. The insert path writes the default metadata keys, and Pydantic validates `source`, `metadata` and each `index_status` entry.
- Each document keeps its own `source`. There is no unique key on the fingerprint.
- Identifier lookup (`find_document_by_doc_identifier`) matches `documents.id`, `workflows.temporal_workflow_id`, `source->>'fingerprint'`, `source->>'original_filename'`, an index `doc_id` and the legacy `md5(source->>'fingerprint')`.
- No `deleted_at`: soft delete is `is_disabled`, hard delete is `purge_document`.

---

## 5. `workflows`

One row per processing run of a document: the main pipeline run, retries, reingest and PROD promotion. Holds all processing state.

Indexes: `(document_id, started_at DESC)`, `(status, started_at DESC)`, `(temporal_workflow_id)`.

| Column | Type | Constraints / Default | Purpose |
|---|---|---|---|
| id | UUID | PRIMARY KEY, DEFAULT gen_random_uuid() | Workflow ID. |
| document_id | UUID | NOT NULL, FK → `documents(id)` ON DELETE CASCADE | Document the workflow runs against. |
| workflow_type | TEXT | NOT NULL | Kind of run (`WorkflowType`). |
| temporal_workflow_id | TEXT | NOT NULL | Temporal workflow ID. `doc-<md5>` for the main run, suffixed for retries. Not unique (a restart reuses the ID). Used by the API, UI and Temporal. |
| status | TEXT | NOT NULL DEFAULT 'running' | `running`, `waiting_review`, `completed` or `failed` (`WorkflowStatus`). |
| current_stage | TEXT | NOT NULL DEFAULT 'registered' | Stage the workflow is in now (`DocumentStage`). This is the document's stage when the workflow is the latest. |
| stage_timestamps | JSONB | NOT NULL DEFAULT '{}', CHECK object with only keys `ocr`, `translation`, `chunks` | When each stage first completed in this run, ISO-8601 UTC. Set-once per key. |
| stop_after_ocr | BOOLEAN | NOT NULL DEFAULT FALSE | Halt after OCR (OCR-only batch runs). |
| error_message | TEXT | | Failure reason when status is `failed`. |
| config | JSONB | NOT NULL DEFAULT '{}', CHECK object | Settings the run used, plus `chunking_progress` while chunking. |
| started_by_user_id | UUID | FK → `users(id)` ON DELETE SET NULL | Who started it. For `promote_prod`, the person who requested promotion. NULL for system-started runs. |
| started_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | When the workflow started. Plays the role of `created_at`. |
| completed_at | TIMESTAMPTZ | | When it completed or failed. |
| updated_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | Last change (trigger). Progress updates touch the row often. |

13 columns.

Notes
- `stage_timestamps` is set-once: `stage_timestamps = jsonb_build_object('ocr', $1) || stage_timestamps` (right side wins, so existing keys are kept).
- Progress updates are atomic: `UPDATE ... SET config = config || jsonb_build_object('chunking_progress', ...)`.
- A PROD request inserts a `promote_prod` workflow in `waiting_review`, with `started_by_user_id` set to the requester and `started_at` to the request time. Approval moves it to `running`; when approval starts a `PromoteToProdWorkflow`, that run takes over the waiting row instead of adding a second one.
- The current workflow of a document is the latest by `started_at`. A retry started while an older run is still `running` takes over the stage display, and so does a retry on a `completed` document: its stage goes back to the retry's stage until the retry finishes.
- No CHECK ties `completed_at` to status: `reconcile_materialized_state` sets `waiting_review` without clearing `completed_at`.
- `cancelled` is not a status because nothing writes it.
- Do not copy the uploader snapshot into `config`; `started_by_user_id` and `audit_logs` cover it.

### View: `document_current_workflow`

Latest workflow per document. Stores nothing.

```sql
SELECT DISTINCT ON (document_id) *
FROM workflows
ORDER BY document_id, started_at DESC, id DESC;
```

---

## 6. `workflow_artifacts`

Ledger of every file a workflow produces or stores. Files live in MinIO; this table records where and what. Append-only.

Indexes: `(workflow_id, created_at DESC)`, `(workflow_id, artifact_type, created_at DESC)`.

| Column | Type | Constraints / Default | Purpose |
|---|---|---|---|
| id | UUID | PRIMARY KEY, DEFAULT gen_random_uuid() | Artifact ID, used in `/documents/{id}/artifacts/{artifact_id}`. |
| workflow_id | UUID | NOT NULL, FK → `workflows(id)` ON DELETE CASCADE | Workflow that produced it. |
| artifact_type | TEXT | NOT NULL, CHECK not blank | Kind of output (`original_upload`, `normalized_pdf`, `normalized_spreadsheet`, `ocr_pages_json`, `translation_pages_json`, `chunk_json_export`, `vector_payload_export`, network publish exchange). Open set, so not an enum. |
| stage | TEXT | | Stage that created it (`DocumentStage`). |
| storage_uri | TEXT | NOT NULL | MinIO location of the blob. |
| mime_type | TEXT | | MIME type. |
| filename | TEXT | | File name of the blob. |
| size_bytes | BIGINT | CHECK ≥ 0 | Blob size. |
| metadata | JSONB | NOT NULL DEFAULT '{}', CHECK object | Chunking config for chunk exports, request and response for network publishes, source path for uploads. |
| created_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | When the artifact was recorded. |

10 columns.

Notes
- Every artifact belongs to a workflow. The upload path creates the first `pipeline` workflow before recording `original_upload`.
- The document is reached through `workflows.document_id`. Per-document listing: `SELECT a.* FROM workflow_artifacts a JOIN workflows w ON w.id = a.workflow_id WHERE w.document_id = $1 ORDER BY a.created_at DESC`.
- The latest original or normalized artifact uses the same join with `artifact_type = 'original_upload'` (or `normalized_%`).
- Deleting a workflow deletes its artifact rows. Blobs in MinIO are not removed by the database; cleanup stays in `purge_document`.
- No `updated_at`: rows are append-only.
- Chunking run history lives in `chunk_json_export` metadata.

---

## 7. `pages`

OCR layer and translations of a page.

Unique: `(document_id, page_number)`.

| Column | Type | Constraints / Default | Purpose |
|---|---|---|---|
| id | UUID | PRIMARY KEY, DEFAULT gen_random_uuid() | Page ID. |
| document_id | UUID | NOT NULL, FK → `documents(id)` ON DELETE CASCADE | Owning document. |
| page_number | INTEGER | NOT NULL, CHECK ≥ 1 | 1-based page position. |
| original_markdown | TEXT | | Raw OCR output, kept unchanged. |
| edited_markdown | TEXT | | Reviewer-corrected OCR text. Used instead of the original when present. |
| translations | JSONB | NOT NULL DEFAULT '{}', CHECK `jsonb_typeof(translations) = 'object'` | Translations of this page, keyed by target language. See shape below. |
| metadata | JSONB | NOT NULL DEFAULT '{"is_reviewed": false}', CHECK object | OCR review and language fields. See keys below. |
| created_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | When the page row was first created. |
| updated_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | Last change (trigger). |

### `metadata` keys

| Key | Type | Default | Purpose |
|---|---|---|---|
| is_reviewed | boolean | false | OCR review done. |
| reviewer_notes | string | absent | Reviewer comments on the OCR text. |
| detected_language | string | absent | Language code detected. Non-`en` means the page needs translation. |

SQL does not check these types; Pydantic does. The review stage gate counts reviewed pages with `(metadata->>'is_reviewed')::boolean`, and the translation step finds pages to translate with `metadata->>'detected_language'`. Add expression indexes on both if those queries are slow: `((metadata->>'is_reviewed')::boolean)` and `((metadata->>'detected_language'))`.

### `translations`

```json
{
  "gu": {
    "translated_markdown": "...",
    "edited_translation": null,
    "is_reviewed": false,
    "reviewer_notes": null,
    "provider": "azure",
    "model": "gpt-5.4-mini",
    "translated_at": "2026-10-19T06:14:00Z"
  }
}
```

- Key is the target language. Several languages per page are possible.
- Pages that need no translation (English) keep `'{}'`. An entry exists only for pages actually translated.
- `edited_translation` takes precedence over `translated_markdown` when present.
- Writes target one path, for example `SET translations = jsonb_set(translations, '{gu,edited_translation}', to_jsonb($1::text))`.
- Re-running OCR on a page resets its `translations` to `'{}'`, since the page text changed.
- Counting translated pages is `WHERE translations <> '{}'`. Finding unreviewed translations needs `jsonb_each` over the small per-page value.

Notes
- `save_pages` replaces the whole row, so it must pass `translations` through unchanged unless the translation activity is writing it. Otherwise an OCR save would wipe translations.
- `save_pages` is `INSERT ... ON CONFLICT (document_id, page_number) DO UPDATE SET <all columns>`. It is called during OCR segments, after OCR, after translation and at persist time, and each call replaces the whole row, so the caller passes every field.
- `save_pages` also passes `metadata` through whole. Update single keys with `metadata = metadata || jsonb_build_object('is_reviewed', true)` instead of rewriting the object.
- Page edits are audited in `audit_logs` (`page_edit`, `page_reset`), so no edit-history columns.
- Pages belong to a document, not to the source file: edits, review flags and OCR retries are per document.

---

## 8. `chunks`

Unique: `(document_id, chunk_number)`.

| Column | Type | Constraints / Default | Purpose |
|---|---|---|---|
| id | UUID | PRIMARY KEY, DEFAULT gen_random_uuid() | Chunk ID. |
| document_id | UUID | NOT NULL, FK → `documents(id)` ON DELETE CASCADE | Owning document. |
| chunk_number | INTEGER | NOT NULL, CHECK ≥ 1 | Order within the document. |
| original_text | TEXT | NOT NULL | Chunk text as produced by the chunker. |
| edited_text | TEXT | | Reviewer-edited text. Indexed when present. |
| token_count | INTEGER | NOT NULL DEFAULT 0, CHECK ≥ 0 | Length in tokens. |
| page_start | INTEGER | NOT NULL DEFAULT 1, CHECK ≥ 1 | First page covered. Kept for display and provenance links. |
| page_end | INTEGER | NOT NULL DEFAULT 1, CHECK ≥ `page_start` | Last page covered. |
| source_spans | JSONB | NOT NULL DEFAULT '[]', CHECK array | List of `{page_number, start_char, end_char}` pointing back into the page text. |
| section_title | TEXT | | Heading of the section. |
| content_type | TEXT | | Kind of content (`heading`, `body`, ...). Only those two seen in the chunkers, so left as `TEXT`. |
| is_reference | BOOLEAN | NOT NULL DEFAULT FALSE | Reference material. Search can exclude it. |
| is_reviewed | BOOLEAN | NOT NULL DEFAULT FALSE | Reviewed by a human. |
| is_excluded | BOOLEAN | NOT NULL DEFAULT FALSE | Excluded from indexing. |
| reviewer_notes | TEXT | | Reviewer comments. |
| created_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | Created. |
| updated_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | Last change (trigger). |

Notes
- Chunking provider, model, config and run number are stored once per document in `documents.metadata.chunking`, not per chunk.
- The distinct page numbers a chunk draws from are derived from `source_spans`.
- Re-chunk runs in one transaction: delete the document's chunks, insert the new ones, update `metadata.chunking` (with `run_number` incremented) (the chunk count is counted from `chunks`, not stored). Chunk UUIDs change on every re-chunk; nothing references `chunks.id` (audit uses chunk number).
- `search_chunks` does a substring match on `lower(coalesce(edited_text, original_text))`, served by a trigram GIN index on that expression (the migration creates the `pg_trgm` extension).

> **Why `pages` and `chunks` stay separate tables:** a page is a physical unit from OCR and a chunk is a semantic unit from the chunker, so their boundaries do not align. One chunk can span several pages and one page can hold several chunks, which makes the relationship many-to-many. Storing page text inside each chunk would copy a page into every chunk it overlaps, so any page edit, OCR retry or translation change would have to be rewritten in all of those chunks.


---

## 9. `audit_logs`

Append-only history of actions. Must outlive documents, so there is no cascade delete.

| Column | Type | Constraints / Default | Purpose |
|---|---|---|---|
| id | UUID | PRIMARY KEY, DEFAULT gen_random_uuid() | Audit entry ID. |
| document_id | UUID | FK → `documents(id)` ON DELETE SET NULL | Document the action applied to. NULL for system-level entries and after a purge. |
| workflow_id | TEXT | | Temporal workflow ID snapshot. Survives a purge. |
| instance_id | UUID | FK → `instances(id)` ON DELETE SET NULL | Provider instance snapshot, so tenant scoping works after the document is gone. |
| action_type | TEXT | NOT NULL, CHECK not blank | What happened (`page_edit`, `purge_document`, ...). 22 values and growing, so not an enum. |
| entity_type | TEXT | | `document`, `page`, `chunk`, `setting` or `catalog` (`AuditEntityType`). |
| entity_number | INTEGER | | Page number or chunk number the action applied to. |
| field_name | TEXT | | Field that changed. |
| old_value | TEXT | | Value before the change. |
| new_value | TEXT | | Value after the change. |
| metadata | JSONB | NOT NULL DEFAULT '{}', CHECK object | Extra context. |
| actor_user_id | UUID | FK → `users(id)` ON DELETE SET NULL | Who acted. NULL for system actions. |
| actor | JSONB | NOT NULL DEFAULT '{}', CHECK object | Identity at the time: `{username, email, roles[]}`. |
| created_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | When it happened. |

Table CHECK: `entity_type IN ('setting','catalog') OR workflow_id IS NOT NULL`.

Indexes: `(document_id, created_at DESC)`, `(instance_id, created_at DESC)`, `(created_at DESC)`, `(action_type, created_at DESC)`, `(created_at DESC) WHERE entity_type = 'setting'`.

Notes
- The FK to `documents` is `SET NULL`, not cascade. A cascade would erase the audit trail on purge, and a strict FK would reject setting and catalog entries that have no document.
- Setting and catalog entries have NULL `document_id` and `workflow_id`, with `entity_type` set to `setting` or `catalog`.
- `instance_id` is a snapshot of the document's provider instance, so a purged document's history stays visible only to its own tenant.
- `entity_number` holds a page or chunk number, not an ID (chunk UUIDs would dangle after a re-chunk).
- `get_settings_audit_logs` is `WHERE entity_type = 'setting'`.
- `log_audit` fills `instance_id`, resolves the actor to a `users` row and builds the `actor` JSON.
- No `updated_at`: entries are immutable.
- The table only grows. Monthly partitioning is possible later without changing this design.

---

## 10. `search_settings`

Single row of search configuration.

Unique: `singleton`.

| Column | Type | Constraints / Default | Keys inside | Purpose |
|---|---|---|---|---|
| id | UUID | PRIMARY KEY, DEFAULT gen_random_uuid() | | Row ID. |
| singleton | BOOLEAN | NOT NULL DEFAULT TRUE UNIQUE, CHECK `singleton` | | Guarantees only one row. |
| retrieval | JSONB | NOT NULL DEFAULT '{}', CHECK object | `searchMethod`, `alpha`, `rankingMethod`, `hybridRrfK` | How semantic and keyword matching are combined and ranked. |
| candidates | JSONB | NOT NULL DEFAULT '{}', CHECK object | `indexName`, `efSearch`, `candidateCap`, `candidateMultiplier` | Which vector index is searched and how many candidates are pulled. |
| results | JSONB | NOT NULL DEFAULT '{}', CHECK object | `limit`, `maxChunksPerDoc`, `excludeReference`, `showHighlights`, `rerankMode` | How the final result list is shaped. |
| query | JSONB | NOT NULL DEFAULT '{}', CHECK object | `useE5Prefix`, `queryExpansionProfile` | How the query is prepared before searching. |
| created_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | | Created. |
| updated_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | | Last change (trigger). |

15 settings in four groups (4 + 4 + 5 + 2). All are global, with no per-tenant scope.

Notes
- The database enforces only "is a JSON object". Value rules live in Pydantic: `SearchSettings` has four nested sub-models (`alpha` 0 to 1, integers ≥ 1, `searchMethod` in `TENSOR|LEXICAL|HYBRID`, `rankingMethod` in `rrf|normalize_linear`, unknown keys rejected).
- The API stays flat. `db.py` keeps one dict mapping each flat key to its group; reads flatten the columns and writes route each key to its column.
- Defaults live only in the Pydantic models. Reads merge stored JSON over the defaults. The default for `alpha` is 0.6.
- Partial update is an atomic merge per group: `UPDATE search_settings SET retrieval = retrieval || $1::jsonb`.
- Audit: one `audit_logs` row per changed key, `entity_type = 'setting'`, `field_name` such as `retrieval.alpha`.
- `rerankMode` has at least `none` and `bm25lite`; the full set was not seen, so no enum.
- Adding a setting to an existing group needs no migration; a new group needs a column.

---

## 11. Retired email challenges

Migration `0015` retires the legacy `email_otps` table. It waits for outstanding challenges to expire by refusing the migration while any unexpired row remains. The SQLite importer counts old challenges without copying their hashes. Keycloak authenticates console users. Migration `0018` adds `email_sign_in_limits` for the one-minute send cooldown. It stores an HMAC of the normalized address and the next allowed send time. The API removes expired rows as it handles new requests. Keycloak holds the one-time code and attempt counter.

The same migration moves the first raw JSON item observed on legacy crawl replay into four optional columns on `source_intake_receipts`. It preserves the original payload, receipt identity, digest, and observation time. Stop older API and worker processes before upgrading their schema.

---

## View: `catalog_status`

Computed from `documents`, `catalogs`, `instances`, `workflows` and `document_current_workflow`. Stores nothing. One row per document. The `/catalog/v1/*` snapshot API reads it for the scheme type (`catalogs.type` of the scheme kind), and its fields are named `scheme_code`, `scheme_name` and so on, so consumers see no change.

| Column | Type | How it is computed |
|---|---|---|
| document_id | UUID | `documents.id`. |
| catalog_id | UUID | Matching legacy catalog, if present. |
| instance_id | UUID | `documents.provider_id`. |
| instance_name | TEXT | `instances.name`. |
| type | TEXT | `catalogs.type`. |
| code | TEXT | `documents.code`. |
| name | TEXT | `documents.name`. |
| aliases | TEXT[] | `documents.aliases`. |
| status | TEXT | `pending_reindex` if the document is live and `metadata.reindex_required` is true. Else `live` if live. Else `pending_prod` if the latest workflow's stage is `approval_for_prod` and the document is not disabled. Else `disabled`. |
| workflow_id | TEXT | `temporal_workflow_id` of the document's earliest `pipeline` workflow. |
| chunk_count | INTEGER | Count of the document's `chunks`. |
| network_visible | BOOLEAN | `metadata.network_visible`, true when absent. |
| promoted_at | TIMESTAMPTZ | `metadata.index_status.prod.last_indexed_at`, only for live documents. |

"Live" means the latest workflow's `current_stage = 'completed'`, `is_disabled` is false and `metadata.catalog_visible` is true. There are no bootstrap rows: a scheme appears only once a document exists.

---

## Retired shared-key advisory intake

The migration kit archives old webhook rows in `legacy_import_evidence`. Its archived PostgreSQL upgrade `0022` retains the full original row, keyed by source and external ID, and removes `webhook_records`. The SQLite importer also preserves raw legacy rows in the archive. Current webhook processing uses `webhook_sources`, `webhook_mapping_versions`, `webhook_deliveries`, and `source_intake_receipts` for delivery and document history.

New deliveries keep their exact request bytes in object storage. The nullable `webhook_deliveries.raw_storage_uri` points to that object. Older rows retain the parsed JSON, which cannot recover the original formatting or key order.

---

## Revisions and network releases

`document_revisions` freezes the content and its checksum. `network_release_staging` associates a revision with a network, collection, provider identity, and the release generation observed when indexing began. The release worker compares that generation before changing the active pointer in `network_releases`. Search checks the active pointer and excludes staged, superseded, and withdrawn revisions.

The worker records a delivery intent under the network and transaction ID in `publication_deliveries`. It stores the publisher's receipt before writing the exchange artifact or activating the revision. A retry reuses a recorded receipt and the exchange artifact ID. If the worker loses the response before saving it, a retry may send another request with the same transaction ID.

## Schema pack versions

`schema_pack_versions` stores each approved bundle under its network ID, capability, and SHA-256 digest. Installing a new digest keeps earlier versions available. The upload and structured intake paths validate supplied pack attributes against the selected digest. The revision snapshot retains the selection and attributes for later validation.

Prepare a local JSON or YAML manifest with `capability`, `version`, `entrypoint`, `component`, and `files`. The `files` object maps each schema's HTTPS identifier to a path under the manifest directory, including every referenced dependency. The validator resolves references from those files without fetching remote schemas.

Run `uv run python -m scripts.install_schema_pack manifest.json` to print the checked digest. Add `--install-for-network NETWORK_ID` to register the bundle in PostgreSQL. With `--verify-registry`, the command also checks the capability, version, and entrypoint against the configured network's active Registry entry. Supply the resulting `network_id`, `capability`, `digest`, and `attributes` in the `schema_pack` object inside `knowledge_metadata`.

A Beckn catalog profile can also pin its outbound schema. Add a `schema_pack` object containing `capability` and `digest` to the kind's projection. For managed publication, the worker checks the catalog's `@type` and validates its resource attributes against that pack on the destination network before sending the catalog.

## `schema_migrations`

The runner creates the ledger with `version` (the `NNNN` prefix and primary key), `name`, and `applied_at`. It installs all six bootstrap files and records one `0022` baseline marker in the same transaction. A pre-release database already at `0022` keeps its existing ledger. The runner takes a session advisory lock across startup and commits each later migration in its own transaction.

---

## Implementation notes

- **Identifiers.** `pipeline.db` accepts the document UUID, Temporal workflow ID, fingerprint, filename, index `doc_id` or legacy `md5(fingerprint)` doc ID through `find_document_by_doc_identifier`. Documents, jobs and artifacts come back as flat dicts. `id` is the UUID and `workflow_id` is the Temporal workflow ID of the document's main run, the earliest `pipeline` or `ocr_only` workflow. The API rewrites `/documents/{id}/...` to the workflow ID before routing because handlers use it to signal and query Temporal.
- **Jobs.** The API's "jobs" and "runs" are `workflows` rows; job ids and artifact ids are the UUIDs of `workflows` and `workflow_artifacts`. `latest_job_id`, `original_artifact_id` and `normalized_artifact_id` are derived by join, not stored.
- **Instance.** The API returns an `instance` string on documents, runs, queue rows and audit entries. Document queries join `documents.provider_id` to `instances.id` and compare `lower(instances.name)`.
- **Catalog version.** `/catalog/v1/version` counts `audit_logs` rows with `entity_type = 'catalog'` and `action_type = 'catalog_version_bump'`, so it only grows. `updated_at` is the newest such row.
- **Document stage.** The stage a document shows is `document_current_workflow.current_stage`; `update_document_stage` writes to that workflow.
- **Page translations.** `save_pages` keeps stored translations unless the page carries `translations` (replaces them) or flat `translated_*` fields (merged into `translation_target_language`). A page whose OCR text changed loses its translations.
- **Users.** Rows are upserted from the auth dependency on write requests (at most once per five minutes per user) and from uploads, workflow starts and audit entries. `local-admin` is seeded by the migration. A user's `instance_id` is set from the tenant codes in the token.
