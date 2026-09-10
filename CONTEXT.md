# Bharat Vistaar Docs Pipeline

A review-driven document ingestion pipeline: documents move through OCR, translation, and chunking review gates, then get indexed into a vector store (DEV, then optionally PROD) and announced to the wider partner network.

## Language

**Publish to Dev**:
The operator action that approves a document out of `ready_for_ingestion` and lets it enter DEV vector-index ingestion. Implemented as `Permission.APPROVE_INGESTION`.
_Avoid_: "publish" alone — ambiguous with Publish to Network.

**Publish to Network**:
The pipeline stage that runs only after a document's PROD promotion succeeds, and POSTs a Beckn-shaped `publish` envelope to an external Discovery Service. Skipped entirely when PROD is disabled (`DISABLE_PROD_SETTING=true`) — a document never reaches the network without first promoting to PROD. The envelope it sends is kind-level (see Knowledge Kind), not document-level: it does not make any single document searchable, only re-announces that this provider serves that kind at all.
_Avoid_: "publish" alone — ambiguous with Publish to Dev.

**Master Catalog**:
The global, Postgres-backed registry of prompt/tool metadata (any content type: scheme, advisory, etc.), synced to Redis so the AI layer can pick up entries without a redeploy.
_Avoid_: "catalog" alone — ambiguous with Scheme Catalog and the Network Catalog Envelope.

**Scheme Catalog**:
The SQLite-backed, scheme-only registry that aggregates one or more documents sharing a `scheme_code`. Backs the pull-based `GET /catalog/v1/*` API that other BAPs poll.
_Avoid_: "catalog" alone — ambiguous with Master Catalog and the Network Catalog Envelope.

**Network Catalog Envelope**:
The Beckn wire-protocol `message.catalogs` object sent by Publish to Network to the Discovery Service. Structurally and semantically unrelated to the Master Catalog and Scheme Catalog tables — do not conflate the three.
_Avoid_: "catalog" alone.

**Knowledge Kind**:
What a document *is* to the network — `advisory`, `scheme`, `video`, or an operator-entered slug — held in `documents.document_kind`. Asserted by a reviewer during the pipeline, never inferred from the file, and absent until then (every document starts as the default `document`). Only `advisory` and `scheme` map to a Network Catalog Envelope; the rest publish nothing.
_Avoid_: "document type" — collides with `source_type`/`canonical_input_type`, which describe the input *format* (pdf, spreadsheet). Also avoid saying an advisory is "uploaded": what is uploaded is a file, which only becomes an advisory when someone classifies it.

**network_visible**:
An operator-controlled flag on a document/scheme (not a pipeline stage) that gates whether it's exposed to other BAPs through the pull-based Scheme Catalog snapshot. Independent of Publish to Network.
