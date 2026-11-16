"""Activities for writing and maintaining search projections."""

import asyncio
import os
from datetime import datetime
from uuid import uuid4

from temporalio import activity
from temporalio.exceptions import ApplicationError

from pipeline.search.index_identity import projection_doc_id
from pipeline.network.release_policy import (
    index_only_mode,
    managed_release_targets,
    upload_release_target,
)
from pipeline.network.network_provider import InvalidProviderIdError
from pipeline.network.network_publisher import PublishConfigError, get_network_adapter
from pipeline.search.passage_projection import (
    _draft_revision_passages,
    _prepare_records,
    _release_passages,
    _revision_snapshot,
    _scheme_fields_from_doc,
    _validity_fields_from_doc,
)
from pipeline.temporal.activities.artifacts import _upload_artifact, _write_json_temp


def _provider_id_from_doc(doc: dict | None, network_id: str | None = None) -> str | None:
    doc = doc or {}
    try:
        kind = (doc.get("kind") or "document").strip().lower()
        provider_id = get_network_adapter(network_id).provider_id(kind, doc.get("instance"))
        if provider_id is not None and (
            not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 256
            or provider_id != provider_id.lower() or any(char.isspace() or ord(char) < 32 for char in provider_id)
        ):
            raise PublishConfigError("The network adapter returned an invalid search provider identity")
    except (InvalidProviderIdError, PublishConfigError) as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    return provider_id


def _draft_provider_id(doc: dict) -> str | None:
    return doc.get("instance") if index_only_mode() else _provider_id_from_doc(doc)


@activity.defn
async def prepare_for_ingestion(
    document_id: str,
    filename: str,
    chunks: list[dict],
    workflow_id: str | None = None,
    legacy_secondary_title: str | None = None,
    legacy_primary_title: str | None = None,
    description: str = None,
) -> list[dict]:
    """Prepare chunks for vector-index ingestion."""
    from pipeline import db

    activity.logger.info(f"Preparing {len(chunks)} chunks for ingestion")
    doc = db.get_document(workflow_id) if workflow_id else None
    if doc:
        document_id = projection_doc_id(doc, document_id)
    records = _prepare_records(
        document_id,
        filename,
        chunks,
        workflow_id=workflow_id,
        description=description,
        instance=(doc or {}).get("instance"),
        **_validity_fields_from_doc(doc),
        provider_id=_provider_id_from_doc(doc),
        display_name=legacy_primary_title or (doc or {}).get("name") or legacy_secondary_title,
        **_scheme_fields_from_doc(doc),
    )
    activity.logger.info(f"Prepared {len(records)} records")
    return records


@activity.defn
async def ingest_to_vector_db(
    records: list[dict],
    index_name: str = "documents-index",
    batch_size: int = 10,
) -> dict:
    """Ingest records into the Qdrant vector index."""
    from pipeline.vector_store import get_default_index_name, get_vector_store

    if not index_name or index_name == "documents-index":
        index_name = get_default_index_name() or index_name

    activity.logger.info(
        "Ingesting %s records to Qdrant collection %s",
        len(records),
        index_name,
    )
    store = get_vector_store()
    # store.upsert() runs CPU-bound embedding synchronously (model.encode()) —
    # off the event loop so it doesn't stall Temporal's query/heartbeat handling.
    result = await asyncio.to_thread(
        store.upsert, index_name, records, batch_size=max(batch_size, 8)
    )
    activity.logger.info("Qdrant ingestion complete: %s", result.get("index_stats"))
    return result


@activity.defn
async def promote_document_to_prod_qdrant(
    workflow_id: str,
    document_id: str,
    filename: str,
    batch_size: int = 10,
) -> dict:
    """Re-upsert document chunks into the PROD Qdrant collection (from Postgres)."""
    from pipeline import db
    from pipeline.vector_store.qdrant_store import QdrantVectorStore, get_qdrant_client

    prod_url = (os.environ.get("PROD_VECTOR_DB_URL") or "").strip()
    prod_key = (os.environ.get("PROD_VECTOR_DB_API_KEY") or "").strip() or None
    prod_collection = (
        os.environ.get("PROD_VECTOR_DB_COLLECTION_NAME")
        or os.environ.get("VECTOR_DB_COLLECTION_NAME")
        or "documents-index"
    ).strip()
    if not prod_url:
        raise RuntimeError("PROD_VECTOR_DB_URL is required to promote documents to prod")

    timeout = float(
        os.environ.get("PROD_VECTOR_DB_TIMEOUT_SECONDS")
        or os.environ.get("VECTOR_DB_TIMEOUT_SECONDS")
        or "30"
    )
    # Temporarily honor prod timeout for this client build via env used by get_qdrant_client
    prev_timeout = os.environ.get("VECTOR_DB_TIMEOUT_SECONDS")
    os.environ["VECTOR_DB_TIMEOUT_SECONDS"] = str(timeout)
    try:
        client = get_qdrant_client(url=prod_url, api_key=prod_key)
    finally:
        if prev_timeout is None:
            os.environ.pop("VECTOR_DB_TIMEOUT_SECONDS", None)
        else:
            os.environ["VECTOR_DB_TIMEOUT_SECONDS"] = prev_timeout

    chunks = db.get_chunks(workflow_id, include_excluded=True)
    doc = db.get_document(workflow_id) or {}
    document_id = projection_doc_id(doc, document_id)
    scheme_kwargs = _scheme_fields_from_doc(doc)
    kind = (scheme_kwargs.get("kind") or "document").strip().lower()
    is_scheme = kind == "scheme" and bool((scheme_kwargs.get("code") or "").strip())

    if is_scheme:
        prod_url = (
            (os.environ.get("PROD_SCHEME_VECTOR_DB_URL") or "").strip()
            or prod_url
        )
        prod_key = (
            (os.environ.get("PROD_SCHEME_VECTOR_DB_API_KEY") or "").strip()
            or prod_key
        )
        prod_collection = (
            os.environ.get("PROD_SCHEME_VECTOR_DB_COLLECTION_NAME") or "schemes-index"
        ).strip()
        docs_default = (
            os.environ.get("PROD_VECTOR_DB_COLLECTION_NAME")
            or os.environ.get("VECTOR_DB_COLLECTION_NAME")
            or "documents-index"
        ).strip()
        if prod_collection == docs_default:
            raise RuntimeError(
                "Scheme promote refused: PROD_SCHEME_VECTOR_DB_COLLECTION_NAME must not "
                f"equal documents collection ({docs_default}). Set schemes-index."
            )
        # Rebuild client if scheme URL differs
        scheme_url = (os.environ.get("PROD_SCHEME_VECTOR_DB_URL") or "").strip()
        if scheme_url and scheme_url != (os.environ.get("PROD_VECTOR_DB_URL") or "").strip():
            client = get_qdrant_client(url=scheme_url, api_key=prod_key)

    records = _prepare_records(
        document_id,
        filename,
        chunks,
        workflow_id=workflow_id,
        instance=doc.get("instance"),
        **scheme_kwargs,
        **_validity_fields_from_doc(doc),
        provider_id=_provider_id_from_doc(doc),
        display_name=doc.get("name"),
    )
    activity.logger.info(
        "Promoting %s records to PROD Qdrant collection %s (kind=%s)",
        len(records),
        prod_collection,
        kind,
    )
    store = QdrantVectorStore(client=client)
    result = await asyncio.to_thread(
        store.upsert, prod_collection, records, batch_size=max(batch_size, 8)
    )
    activity.logger.info("PROD promotion complete: %s", result.get("index_stats"))
    db.upsert_document_index_status(
        workflow_id=workflow_id, index_name=prod_collection, doc_id=document_id,
        chunk_count_indexed=result.get("records_ingested", 0),
        last_indexed_at=datetime.utcnow().isoformat(), schema_version="passage-v2",
        status="indexed", details=result.get("index_stats"),
    )
    await _drop_superseded_points("prod", workflow_id, doc, document_id, prod_collection, store=store)
    db.log_audit(
        workflow_id=workflow_id,
        document_id=document_id,
        action_type="promote_to_prod",
        entity_type="document",
        metadata={
            "collection": prod_collection,
            "records_ingested": result.get("records_ingested", len(records)),
            "prod_qdrant_url": prod_url,
            "kind": kind,
            "code": scheme_kwargs.get("code"),
        },
    )
    catalog_version = None
    if is_scheme:
        try:
            from pipeline.network.catalog.scheme_catalog import on_scheme_document_promoted

            catalog_version = on_scheme_document_promoted(workflow_id)
        except Exception as exc:
            activity.logger.warning("Catalog update after promote failed: %s", exc)
    return {
        **result,
        "collection": prod_collection,
        "target": "prod",
        "document_kind": kind,
        "catalog_version": catalog_version,
    }


async def _drop_superseded_points(
    env: str,
    workflow_id: str,
    doc: dict,
    current_doc_id: str,
    collection: str,
    store=None,
) -> None:
    """Delete points of earlier file versions (see ``metadata.superseded_doc_ids``).

    Runs after the new content is indexed, so search never has a gap. A failed
    delete keeps the key for the next run and does not fail the ingest.
    """
    from pipeline import db

    previous = ((doc.get("index_status") or {}).get(env) or {}).get("doc_id")
    stale = [
        doc_id
        for doc_id in dict.fromkeys([*(doc.get("superseded_doc_ids") or {}).get(env, []), previous])
        if doc_id and doc_id != current_doc_id
    ]
    if not stale:
        return
    if store is None:
        from pipeline.vector_store import get_vector_store

        store = get_vector_store()
    deleted = []
    for doc_id in stale:
        # Two documents holding the same file share one index key. While another
        # live document is indexed under it the points stay, and the key stays on
        # the list so a later ingest, disable or purge can still remove it.
        if await asyncio.to_thread(db.doc_id_used_by_other_document, doc_id, doc.get("id"), env):
            activity.logger.info(
                "Keeping %s points for %s: another document is indexed under the same key", env, doc_id
            )
            continue
        result = await asyncio.to_thread(store.delete_by_doc_id, collection, doc_id)
        if result.get("error"):
            activity.logger.warning(
                "Could not delete superseded %s points for %s: %s", env, doc_id, result["error"]
            )
        else:
            deleted.append(doc_id)
    if deleted:
        db.clear_superseded_doc_ids(workflow_id, env, deleted)


def _release_target_for_job(
    doc: dict, job: dict | None, workflow_id: str, release_network_id: str,
) -> tuple[str, str] | None:
    if not job or job.get("temporal_workflow_id") != workflow_id:
        return None
    kind = doc.get("kind")
    config = job["config"]
    if not config.get("explicit_release_network"):
        return upload_release_target(kind, provider=doc.get("instance"))
    if config.get("release_network_id") != release_network_id:
        return None
    collection = managed_release_targets(kind).get(release_network_id)
    if not collection or config.get("release_collection") != collection:
        return None
    return release_network_id, collection


@activity.defn
async def ingest_document_from_db(
    workflow_id: str,
    document_id: str,
    filename: str,
    index_name: str = "documents-index",
    batch_size: int = 10,
    release_network_id: str | None = None,
    source_first: bool = False,
    freeze_draft_revision: bool = False,
    revision_workflow_id: str | None = None,
) -> dict:
    """Prepare and ingest chunks directly from Postgres by workflow_id."""
    from pipeline import db

    if release_network_id or freeze_draft_revision or source_first:
        doc, pages, chunks = db.load_revision_material(workflow_id)
        if not doc or doc.get("is_disabled"):
            raise ApplicationError("The document is unavailable for indexing", non_retryable=True)
        job = db.get_document_job(doc["latest_job_id"])
        source_url = ((job or {}).get("config") or {}).get("source_url")
        if source_url is None and revision_workflow_id:
            source_url = db.get_document_source_url(doc["id"])
    else:
        chunks = db.get_chunks(workflow_id, include_excluded=True)
        doc = db.get_document(workflow_id) or {}
        pages = None
        source_url = None

    if release_network_id:
        try:
            target = _release_target_for_job(doc, job, workflow_id, release_network_id)
        except ValueError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        if target != (release_network_id, index_name):
            raise ApplicationError("The upload release target changed", non_retryable=True)
        if not doc.get("network_visible"):
            raise ApplicationError("The document is unavailable for release", non_retryable=True)
        revision_id = str(uuid4())
        document_id = revision_id
    document_id = projection_doc_id(doc, document_id)
    records = _prepare_records(
        document_id,
        filename,
        chunks,
        workflow_id=workflow_id,
        instance=doc.get("instance"),
        **_scheme_fields_from_doc(doc),
        **_validity_fields_from_doc(doc),
        provider_id=None if release_network_id else _draft_provider_id(doc),
        display_name=doc.get("name"),
        pages=pages,
        source_first=source_first,
        source_url=source_url,
    )
    if release_network_id:
        records = [record for record in records if record["text"].strip()]
        if not records:
            raise ApplicationError("The document has no searchable content", non_retryable=True)
        snapshot = _revision_snapshot(doc, pages, records, document_id, source_url)
        try:
            revision = db.save_revision_snapshot(
                doc["id"], workflow_id, release_network_id, revision_id, snapshot,
                collection=index_name, provider_id=_provider_id_from_doc(doc, release_network_id),
            )
        except ValueError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        document_id = revision["snapshot"]["vector_doc_id"]
        if revision["stage"]["collection"] != index_name:
            raise ApplicationError("The staged release collection changed", non_retryable=True)
        records = _release_passages(revision, release_network_id, revision["stage"]["provider_id"])
    elif index_only_mode():
        records = [record for record in records if record["text"].strip()]
        if not records:
            raise ApplicationError("The document has no searchable content", non_retryable=True)
    if freeze_draft_revision and not release_network_id:
        revision_id = str(uuid4())
        revision_doc_id = projection_doc_id(doc, revision_id)
        passages = _draft_revision_passages(records, revision_doc_id)
        if passages:
            snapshot = _revision_snapshot(doc, pages, passages, revision_doc_id, source_url)
            try:
                db.save_draft_revision_snapshot(doc["id"], revision_workflow_id or workflow_id, revision_id, snapshot)
            except ValueError as exc:
                raise ApplicationError(str(exc), non_retryable=True) from exc
    payload_path = _write_json_temp(records)
    try:
        payload_uri, payload_size, payload_mime = _upload_artifact(
            payload_path, workflow_id, "vector_payload_export", "vector_payload.json"
        )
    finally:
        if os.path.exists(payload_path):
            os.remove(payload_path)
    latest_job = db.get_latest_document_job(workflow_id)
    db.add_document_artifact(
        workflow_id=workflow_id,
        job_id=latest_job["id"] if latest_job else None,
        artifact_type="vector_payload_export",
        stage="ingesting",
        storage_uri=payload_uri,
        mime_type=payload_mime,
        filename="vector_payload.json",
        size_bytes=payload_size,
        metadata={"record_count": len(records), "index_name": index_name},
    )
    result = await ingest_to_vector_db(records, index_name=index_name, batch_size=batch_size)
    if (release_network_id or index_only_mode()) and result.get("records_ingested") != len(records):
        raise ApplicationError("The index did not ingest every eligible passage")
    if index_only_mode() and not release_network_id and result.get("backend") == "qdrant":
        from pipeline.vector_store import get_default_index_name
        from pipeline.vector_store.qdrant_store import QdrantVectorStore

        collection = get_default_index_name() if index_name == "documents-index" else index_name
        await asyncio.to_thread(QdrantVectorStore().remove_superseded_passages, collection, document_id, records)
    db.upsert_document_index_status(
        workflow_id=workflow_id,
        index_name=index_name,
        doc_id=document_id,
        chunk_count_indexed=result.get("records_ingested", 0),
        last_indexed_at=datetime.utcnow().isoformat(),
        last_verified_at=datetime.utcnow().isoformat(),
        schema_version="passage-v2",
        status="indexed",
        details=result.get("index_stats"),
    )
    if not release_network_id:
        await _drop_superseded_points("dev", workflow_id, doc, document_id, index_name)
    return result
