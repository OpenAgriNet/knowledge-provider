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

**Announcement Lifetime**:
The `validity` window (`startDate`/`endDate`) carried on the Network Catalog Envelope, named by the prod approver at `approve-prod` and stored as `documents.network_valid_from` / `network_valid_to`. Start is the approval date; end is prepopulated with the same day and is the approver's to move. Because the envelope is kind-level, the most recently published window is the live one for that whole Knowledge Kind — see `docs/ADR/0005-approver-sets-the-network-catalogs-validity-window.md`.
_Avoid_: "document lifetime" — the document itself is not what expires, the announcement is. Also avoid "expiry": there is a start as well as an end.

**Document Validity**:
The period a document's chunks answer searches in, held as `documents.valid_from` / `documents.valid_to` and stamped onto every chunk's vector payload as `start_date` / `end_date`. Defaulted on upload to the upload day plus one year, then confirmed or moved by the reviewer in the same form that sets the Knowledge Kind. Both ends are inclusive, and search filters on it so an expired or not-yet-started document is never answered from. Chunks ingested before it existed carry neither date and stay searchable — see `docs/ADR/0006-document-validity-filters-search.md`.
_Avoid_: "validity" alone — ambiguous with the Announcement Lifetime, which is a different window (kind-level, `startDate`/`endDate` on the wire, named by the prod approver). This one is document-level, never leaves the vector store, and expires a document's answers rather than an announcement.

**network_visible**:
An operator-controlled flag on a document/scheme (not a pipeline stage) that gates whether it's exposed to other BAPs through the pull-based Scheme Catalog snapshot. Independent of Publish to Network.
