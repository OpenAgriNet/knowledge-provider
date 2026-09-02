# Bharat Vistaar Docs Pipeline

A review-driven document ingestion pipeline: documents move through OCR, translation, and chunking review gates, then get indexed into a vector store (DEV, then optionally PROD) and announced to the wider partner network.

## Language

**Publish to Dev**:
The operator action that approves a document out of `ready_for_ingestion` and lets it enter DEV vector-index ingestion. Implemented as `Permission.APPROVE_INGESTION`.
_Avoid_: "publish" alone — ambiguous with Publish to Network.

**Publish to Network**:
The pipeline stage that runs immediately after DEV ingest completes and POSTs a Beckn-shaped `catalog/publish` envelope to an external Discovery Service. This is the only path the experience layer uses to discover schemas going forward; it is unrelated to Publish to Dev.
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

**network_visible**:
An operator-controlled flag on a document/scheme (not a pipeline stage) that gates whether it's exposed to other BAPs through the pull-based Scheme Catalog snapshot. Independent of Publish to Network.
