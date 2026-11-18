"""Document lifecycle, retries, and reconciliation routes."""

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from pipeline import db
from pipeline.api import api as _api
from pipeline.api.api import (
    _actor_from_user,
    _assert_upload_start_resolved,
    _instance_scope_for_user,
    _log_audit,
    _mark_reindex_required,
    _require_document_for_user,
    delete_chunks_from_index,
)
from pipeline.auth.deps import CurrentUser, RequireAdmin, RequirePipeline, RequireReview
from pipeline.auth.permissions import Permission
from pipeline.documents import display_name as display_name_rules
from pipeline.documents.models import ReindexStateRequest, SchemeMetadataUpdate
from pipeline.network.catalog import scheme_catalog
from pipeline.network.release_policy import index_only_mode, intake_index_options
from pipeline.object_storage import is_object_uri, parse_object_uri
from pipeline.search.index_identity import projection_doc_id, recorded_doc_id
from pipeline.temporal.workflows import (
    ChunkingOnlyWorkflow,
    OcrOnlyWorkflow,
    ReingestionWorkflow,
    TranslationOnlyWorkflow,
)

router = APIRouter()


def _vector_doc_ids(doc: dict, env: str) -> list[str]:
    """Index keys to delete for a document: its current key plus keys of earlier file versions in ``env``.

    A key another live document is indexed under in ``env`` (the same file
    uploaded twice) is left out, since deleting it would remove that document's
    vectors too.
    """
    ids = [recorded_doc_id(doc, env)]
    from pipeline.vector_store import get_default_index_name

    if env == get_default_index_name() and index_only_mode() and doc.get("id") and doc.get("document_id"):
        ids.append(projection_doc_id(doc))
    ids.extend((doc.get("superseded_doc_ids") or {}).get(env, []))
    return [
        i
        for i in dict.fromkeys(i for i in ids if i)
        if not db.doc_id_used_by_other_document(i, doc.get("id"), env)
    ]


def _delete_all_doc_ids(store, collection: str, doc_ids: list[str]) -> dict:
    results = [store.delete_by_doc_id(collection, doc_id) for doc_id in doc_ids]
    if len(results) == 1:
        return results[0]
    errors = [r["error"] for r in results if r.get("error")]
    summary = {"deleted": 0 if errors else -1, "doc_ids": doc_ids, "results": results}
    if errors:
        summary["error"] = "; ".join(errors)
    return summary


def _delete_document_points_from_prod(doc: dict) -> dict:
    """Best-effort delete of a promoted (non-scheme) document's vectors from PROD documents-index."""
    collection = (os.environ.get("PROD_VECTOR_DB_COLLECTION_NAME") or os.environ.get("VECTOR_DB_COLLECTION_NAME") or "documents-index").strip()
    doc_ids = _vector_doc_ids(doc, collection)
    if not doc_ids:
        return {"deleted": False, "reason": "no_doc_id"}
    prod_url = (os.environ.get("PROD_VECTOR_DB_URL") or "").strip()
    if not prod_url:
        return {"deleted": False, "reason": "no_prod_qdrant_url"}
    prod_key = (os.environ.get("PROD_VECTOR_DB_API_KEY") or "").strip() or None
    collection = (
        os.environ.get("PROD_VECTOR_DB_COLLECTION_NAME")
        or os.environ.get("VECTOR_DB_COLLECTION_NAME")
        or "documents-index"
    ).strip()
    try:
        from pipeline.vector_store.qdrant_store import QdrantVectorStore, get_qdrant_client

        client = get_qdrant_client(url=prod_url, api_key=prod_key)
        store = QdrantVectorStore(client=client)
        return _delete_all_doc_ids(store, collection, doc_ids)
    except Exception as exc:
        logging.exception("prod vector delete failed doc_ids=%s collection=%s", doc_ids, collection)
        return {"deleted": False, "error": str(exc), "collection": collection}


def _delete_scheme_points_from_prod(doc: dict) -> dict:
    """Best-effort delete of scheme vectors from PROD schemes-index."""
    collection = (os.environ.get("PROD_SCHEME_VECTOR_DB_COLLECTION_NAME") or "schemes-index").strip()
    doc_ids = _vector_doc_ids(doc, collection)
    if not doc_ids:
        return {"deleted": False, "reason": "no_doc_id"}
    prod_url = (
        (os.environ.get("PROD_SCHEME_VECTOR_DB_URL") or "").strip()
        or (os.environ.get("PROD_VECTOR_DB_URL") or "").strip()
    )
    if not prod_url:
        return {"deleted": False, "reason": "no_prod_qdrant_url"}
    prod_key = (
        (os.environ.get("PROD_SCHEME_VECTOR_DB_API_KEY") or "").strip()
        or (os.environ.get("PROD_VECTOR_DB_API_KEY") or "").strip()
        or None
    )
    collection = (
        os.environ.get("PROD_SCHEME_VECTOR_DB_COLLECTION_NAME") or "schemes-index"
    ).strip()
    try:
        from pipeline.vector_store.qdrant_store import QdrantVectorStore, get_qdrant_client

        client = get_qdrant_client(url=prod_url, api_key=prod_key)
        store = QdrantVectorStore(client=client)
        return _delete_all_doc_ids(store, collection, doc_ids)
    except Exception as exc:
        logging.exception("prod vector delete failed doc_ids=%s collection=%s", doc_ids, collection)
        return {"deleted": False, "error": str(exc), "collection": collection}

def _delete_stored_artifacts(workflow_id: str) -> dict:
    """Remove every stored object backing a document's artifacts.

    Artifacts are the only part of a document that lives outside Postgres and the
    vector store — the original upload, the normalized PDF, and the OCR /
    translation / chunk JSON snapshots. Nothing else reads them once the
    document is gone, so a hard delete has to sweep them explicitly.
    """
    outcome = {"deleted": 0, "missing": 0, "errors": []}
    try:
        artifacts = db.list_document_artifacts(workflow_id)
    except Exception as exc:
        outcome["errors"].append(f"list artifacts: {exc}")
        return outcome

    for artifact in artifacts:
        storage_uri = (artifact.get("storage_uri") or "").strip()
        if not storage_uri:
            continue
        try:
            if is_object_uri(storage_uri):
                _api.object_store.delete(parse_object_uri(storage_uri))
                outcome["deleted"] += 1
            else:
                path = Path(storage_uri)
                if path.exists():
                    path.unlink()
                    outcome["deleted"] += 1
                else:
                    outcome["missing"] += 1
        except Exception as exc:
            outcome["errors"].append(f"{storage_uri}: {exc}")
    return outcome


@router.delete("/documents/{workflow_id}")
async def disable_document(
    workflow_id: str,
    user: CurrentUser,
    remove_from_search: bool = Query(True),
    purge: bool = Query(False),
    keep_audit: bool = Query(True),
):
    """
    Remove a document — soft delete by default, hard delete with ``purge=true``.

    Both modes:
    - Remove all chunks from the DEV vector index
    - Delete PROD Qdrant points too, if this document was ever promoted
      (schemes-index for scheme documents, documents-index otherwise)
    - For scheme documents, also rebuild the scheme catalog
    - Stop the workflow if it is still running

    Soft delete (default) then marks the document disabled in Postgres and keeps
    every row and artifact, so ``POST /documents/{id}/restore`` can bring it
    back. Use X-Include-Disabled: true in list_documents to see it.

    ``purge=true`` additionally deletes the MinIO artifacts (original upload,
    normalized PDF, OCR/translation/chunk JSON) and every Postgres row (pages,
    chunks, artifacts, jobs, index status, the document itself). This is
    irreversible — restore has nothing left to restore. Audit rows are kept
    unless ``keep_audit=false``, since they record the deletion itself.

    Args:
        workflow_id: The document workflow ID
        remove_from_search: If True (default), removes chunks from the index
        purge: If True, hard delete — also drops MinIO artifacts and Postgres rows
        keep_audit: If False (and purge=True), also deletes the audit trail

    Requires ``admin`` (super admin — any document), or ``delete_own``
    (state admin — only documents they uploaded, inside their own state).
    A purge is irreversible, so it stays super-admin only.
    """
    doc = _require_document_for_user(workflow_id, user)

    if not user.has_permission(Permission.ADMIN):
        if not user.has_permission_for_instance(Permission.DELETE_OWN, doc.get("instance")):
            raise HTTPException(403, "Missing permission: delete_own")
        if purge:
            raise HTTPException(
                403, "purge=true is restricted to super admins; delete without purge instead"
            )
        owner = (doc.get("uploaded_by_user_id") or "").strip()
        if not owner or owner != (user.user_id or "").strip():
            raise HTTPException(403, "You may only delete documents you uploaded")

    _assert_upload_start_resolved(workflow_id)

    # A purge that skipped the index would strand vectors no row points at any
    # more, so "remove from everywhere" always includes the search cleanup.
    if purge:
        remove_from_search = True

    result = {
        "workflow_id": workflow_id,
        "disabled": True,
        "purged": purge,
        "workflow_cancelled": False,
        "workflow_terminated": False,
        "vector_deleted": 0,
        "scheme_qdrant_deleted": None,
        "prod_qdrant_deleted": None,
        "catalog_version": None,
        "stored_artifacts_deleted": None,
        "sqlite_rows_deleted": None,
    }

    # Stop the workflow if still running. A purge terminates rather than
    # cancels: cancellation lets the workflow run its next activity, which
    # would write rows back into the tables we are about to delete.
    try:
        handle = _api.temporal_client.get_workflow_handle(workflow_id)
        if purge:
            await handle.terminate(reason=f"Document purged by {user.username or user.user_id}")
            result["workflow_terminated"] = True
        else:
            await handle.cancel()
            result["workflow_cancelled"] = True
    except Exception:
        pass  # Workflow already completed/cancelled

    # Mark as disabled in Postgres (a purge deletes the row outright below, but
    # this keeps the document hidden if a later step fails partway through)
    db.set_document_disabled(workflow_id, True)

    # Remove from the DEV vector index if requested
    if remove_from_search:
        # The current index key plus keys of earlier file versions still in DEV
        # (see metadata.superseded_doc_ids).
        from pipeline.vector_store import get_default_index_name

        for index, doc_id in enumerate(_vector_doc_ids(doc, get_default_index_name())):
            vector_result = delete_chunks_from_index(doc_id)
            if index == 0:
                result["vector_deleted"] = vector_result.get("deleted", 0)
            if "error" in vector_result:
                result["vector_error"] = vector_result["error"]

        # PROD Qdrant cleanup, if this document was ever promoted. A document
        # promoted as a scheme lives in the scheme index even if its kind was
        # changed since, so the PROD index entry decides, not only the kind.
        kind = (doc.get("kind") or "").strip().lower()
        prod_entry = (doc.get("index_status") or {}).get(scheme_catalog.scheme_collection_name()) or {}
        prod_is_scheme = (
            kind == "scheme"
            or prod_entry.get("index_name") == scheme_catalog.scheme_collection_name()
        )
        if prod_is_scheme:
            q_result = _delete_scheme_points_from_prod(doc)
            result["scheme_qdrant_deleted"] = q_result
            try:
                result["catalog_version"] = scheme_catalog.on_scheme_document_disabled(
                    workflow_id, code=doc.get("code")
                )
            except Exception as exc:
                result["catalog_error"] = str(exc)
        else:
            result["prod_qdrant_deleted"] = _delete_document_points_from_prod(doc)

    # Hard delete: drop the artifacts and the Postgres rows themselves. Ordered
    # storage-then-database so a failure mid-way leaves rows pointing at
    # already-deleted objects (visible, fixable) rather than orphaned objects
    # no row references any more (invisible, unreclaimable).
    if purge:
        result["stored_artifacts_deleted"] = await asyncio.to_thread(
            _delete_stored_artifacts, workflow_id
        )
        # Audit is written before the purge so the trail survives keep_audit=True.
        _log_audit(
            workflow_id=workflow_id,
            action_type="purge_document",
            entity_type="document",
            metadata={
                "filename": doc.get("filename"),
                "code": doc.get("code"),
                "stage": doc.get("stage"),
                "stored_artifacts_deleted": result["stored_artifacts_deleted"],
                "keep_audit": keep_audit,
            },
            user=user,
        )
        result["sqlite_rows_deleted"] = db.purge_document(workflow_id, keep_audit=keep_audit)
        result["disabled"] = False  # nothing left to be disabled
        return result

    # Log audit
    _log_audit(
        workflow_id=workflow_id,
        action_type="disable_document",
        entity_type="document",
        metadata={
            "remove_from_search": remove_from_search,
            "vector_deleted": result["vector_deleted"],
            "scheme_qdrant_deleted": result.get("scheme_qdrant_deleted"),
            "prod_qdrant_deleted": result.get("prod_qdrant_deleted"),
            "code": doc.get("code"),
            "catalog_version": result.get("catalog_version"),
        },
        user=user,
    )

    return result


@router.patch("/documents/{workflow_id}/scheme-metadata")
async def patch_scheme_metadata(
    workflow_id: str,
    body: SchemeMetadataUpdate,
    user: RequireReview,
):
    """
    Set code / name / aliases / validity used by Master Catalog and Qdrant scheme promote.

    On completed docs, renaming the code sets pending_reindex (excluded from live
    vector_schemes until re-promote). Other metadata edits bump catalog version only.
    """
    _require_document_for_user(workflow_id, user, Permission.REVIEW)
    try:
        result = scheme_catalog.apply_scheme_metadata(
            workflow_id,
            code=body.code,
            name=body.name,
            aliases=body.aliases,
            tool_routing=body.tool_routing,
            catalog_visible=body.catalog_visible,
            network_visible=body.network_visible,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            display_name=body.display_name,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except display_name_rules.DisplayNameLocked as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    _log_audit(
        workflow_id=workflow_id,
        action_type="scheme_metadata_update",
        entity_type="document",
        metadata={
            "body": body.model_dump(exclude_none=True),
            "catalog_version": result.get("catalog_version"),
            "pending_reindex": result.get("pending_reindex"),
        },
        user=user,
    )
    doc = result["document"]
    return {
        "workflow_id": workflow_id,
        "kind": doc.get("kind"),
        "code": doc.get("code"),
        "name": doc.get("name"),
        "aliases": list(doc.get("aliases") or []),
        "tool_routing": doc.get("tool_routing"),
        "catalog_visible": bool(int(doc["catalog_visible"])) if doc.get("catalog_visible") is not None else True,
        "network_visible": bool(int(doc["network_visible"])) if doc.get("network_visible") is not None else True,
        "valid_from": doc.get("valid_from"),
        "valid_to": doc.get("valid_to"),
        "catalog_version": result.get("catalog_version"),
        "requires_reindex": result.get("requires_reindex"),
        "pending_reindex": result.get("pending_reindex"),
        "reindex_reason": doc.get("reindex_reason"),
    }


@router.post("/documents/{workflow_id}/restore")
async def restore_document(workflow_id: str, user: RequireAdmin):
    """
    Restore a soft-deleted (disabled) document.

    Note: This only restores the document in Postgres. Chunks that were removed
    from the vector index will NOT be automatically re-indexed. To re-index,
    you would need to re-run the ingestion process.
    """
    _require_document_for_user(workflow_id, user)

    db.set_document_disabled(workflow_id, False)

    # Log audit
    _log_audit(
        workflow_id=workflow_id,
        action_type="restore_document",
        entity_type="document",
        user=user,
    )

    return {"workflow_id": workflow_id, "restored": True}


@router.post("/documents/{workflow_id}/reingest")
async def reingest_document(
    workflow_id: str,
    user: RequirePipeline,
    index_name: str = "documents-index",
):
    """
    Re-ingest a completed document to the vector index.

    Use this to re-ingest documents that completed but weren't properly
    indexed (e.g., due to index schema changes). This starts a lightweight
    workflow that uses chunks already stored in Postgres.

    The document must have chunks stored in Postgres (typically from a
    completed or previously ingested document).
    """
    try:
        index_name, _ = intake_index_options(index_name, False)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    doc = _require_document_for_user(workflow_id, user, Permission.PIPELINE)
    _assert_upload_start_resolved(workflow_id)

    # Get chunks from Postgres
    chunks = db.get_chunks(workflow_id, include_excluded=False)
    if not chunks:
        raise HTTPException(400, "No chunks found for document. The document may need to be reprocessed from scratch.")

    document_id = doc.get("document_id", "")
    filename = doc.get("filename", "")
    page_count = doc.get("page_count", 0)

    # Generate unique workflow ID for re-ingestion
    import time
    reingest_workflow_id = f"{workflow_id}-reingest-{int(time.time())}"

    # Pass the deployment mode in the workflow input to keep replay deterministic.
    args = [document_id, filename, workflow_id, page_count, len(chunks), index_name]
    if index_only_mode():
        args.append(True)
    job_id = db.create_document_job(
        workflow_id=workflow_id,
        job_type="reingest",
        temporal_workflow_id=reingest_workflow_id,
        status="running",
        current_stage="ingesting",
        config={"index_name": index_name, "chunk_count": len(chunks)},
        actor=_actor_from_user(user),
    )
    try:
        await _api.temporal_client.start_workflow(
            ReingestionWorkflow.run, args=args, id=reingest_workflow_id, task_queue=_api.TASK_QUEUE,
        )
    except Exception:
        db.update_document_job(job_id, status="failed", current_stage="failed", error_message="Could not start re-ingestion")
        raise

    # Log audit
    _log_audit(
        workflow_id=workflow_id,
        action_type="reingest_started",
        entity_type="document",
        metadata={"reingest_workflow_id": reingest_workflow_id, "chunk_count": len(chunks)},
        user=user,
    )

    return {
        "workflow_id": workflow_id,
        "reingest_workflow_id": reingest_workflow_id,
        "chunk_count": len(chunks),
        "status": "started"
    }


@router.post("/documents/{workflow_id}/retry-ingestion")
async def retry_ingestion(
    workflow_id: str,
    user: RequirePipeline,
    index_name: str = "documents-index",
):
    """Alias for reingesting a document when search is stale or missing."""
    return await reingest_document(
        workflow_id,
        user=user,
        index_name=index_name,
    )


@router.post("/documents/{workflow_id}/retry-ocr")
async def retry_ocr(workflow_id: str, user: RequirePipeline):
    """Retry OCR for an existing document and stop at OCR review."""
    doc = _require_document_for_user(workflow_id, user, Permission.PIPELINE)
    _assert_upload_start_resolved(workflow_id)
    filepath = doc.get("filepath")
    if not filepath:
        raise HTTPException(400, "Document has no source filepath for OCR retry")
    temporal_workflow_id = f"{workflow_id}-retry-ocr-{int(datetime.utcnow().timestamp())}"
    await _api.temporal_client.start_workflow(
        OcrOnlyWorkflow.run,
        args=[workflow_id, doc["document_id"], doc["filename"], filepath],
        id=temporal_workflow_id,
        task_queue=_api.TASK_QUEUE,
    )
    db.create_document_job(
        workflow_id=workflow_id,
        job_type="ocr_retry",
        temporal_workflow_id=temporal_workflow_id,
        status="running",
        current_stage="ocr_processing",
        config={"source": "api_retry_ocr"},
        actor=_actor_from_user(user),
    )
    db.update_document_fields(workflow_id, error_message=None)
    _log_audit(
        workflow_id=workflow_id,
        action_type="retry_ocr",
        entity_type="document",
        metadata={"temporal_workflow_id": temporal_workflow_id},
        user=user,
    )
    return {"workflow_id": workflow_id, "status": "started", "retry_workflow_id": temporal_workflow_id}


@router.post("/documents/{workflow_id}/retry-translation")
async def retry_translation(workflow_id: str, user: RequirePipeline):
    """Retry translation for an existing document and stop at translation review."""
    doc = _require_document_for_user(workflow_id, user, Permission.PIPELINE)
    _assert_upload_start_resolved(workflow_id)
    if not db.get_pages(workflow_id):
        raise HTTPException(400, "No OCR pages found for translation retry")
    temporal_workflow_id = f"{workflow_id}-retry-translation-{int(datetime.utcnow().timestamp())}"
    await _api.temporal_client.start_workflow(
        TranslationOnlyWorkflow.run,
        args=[workflow_id, doc["document_id"], doc["filename"]],
        id=temporal_workflow_id,
        task_queue=_api.TASK_QUEUE,
    )
    db.create_document_job(
        workflow_id=workflow_id,
        job_type="translation_retry",
        temporal_workflow_id=temporal_workflow_id,
        status="running",
        current_stage="translation_processing",
        config={"source": "api_retry_translation"},
        actor=_actor_from_user(user),
    )
    db.update_document_fields(workflow_id, error_message=None)
    _log_audit(
        workflow_id=workflow_id,
        action_type="retry_translation",
        entity_type="document",
        metadata={"temporal_workflow_id": temporal_workflow_id},
        user=user,
    )
    return {"workflow_id": workflow_id, "status": "started", "retry_workflow_id": temporal_workflow_id}


@router.post("/documents/{workflow_id}/retry-chunking")
async def retry_chunking(
    workflow_id: str,
    user: RequirePipeline,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
):
    """Retry chunking for an existing document and stop at chunk review."""
    doc = _require_document_for_user(workflow_id, user, Permission.PIPELINE)
    _assert_upload_start_resolved(workflow_id)
    if not db.get_pages(workflow_id):
        raise HTTPException(400, "No page content found for chunking retry")
    temporal_workflow_id = f"{workflow_id}-retry-chunking-{int(datetime.utcnow().timestamp())}"
    await _api.temporal_client.start_workflow(
        ChunkingOnlyWorkflow.run,
        args=[
            workflow_id,
            doc["document_id"],
            doc["filename"],
            doc.get("page_count", 0),
            chunk_size,
            chunk_overlap,
            min_tokens,
        ],
        id=temporal_workflow_id,
        task_queue=_api.TASK_QUEUE,
    )
    db.create_document_job(
        workflow_id=workflow_id,
        job_type="chunking_retry",
        temporal_workflow_id=temporal_workflow_id,
        status="running",
        current_stage="chunking",
        config={
            "source": "api_retry_chunking",
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "min_tokens": min_tokens,
        },
        actor=_actor_from_user(user),
    )
    db.update_document_fields(workflow_id, error_message=None)
    _log_audit(
        workflow_id=workflow_id,
        action_type="retry_chunking",
        entity_type="document",
        metadata={"temporal_workflow_id": temporal_workflow_id},
        user=user,
    )
    return {"workflow_id": workflow_id, "status": "started", "retry_workflow_id": temporal_workflow_id}


@router.post("/documents/{workflow_id}/mark-reindex-required")
async def mark_reindex_required(workflow_id: str, payload: ReindexStateRequest, user: RequirePipeline):
    """Mark a document as needing reindex after chunk edits or operational drift."""
    _require_document_for_user(workflow_id, user, Permission.PIPELINE)
    updated = _mark_reindex_required(
        workflow_id,
        payload.reason or "Marked manually for reindex",
        metadata={"source": "api"},
    )
    return {
        "workflow_id": workflow_id,
        "reindex_required": bool(updated.get("reindex_required")) if updated else True,
        "reindex_reason": updated.get("reindex_reason") if updated else payload.reason,
    }


@router.post("/documents/{workflow_id}/clear-reindex-required")
async def clear_reindex_required(workflow_id: str, user: RequirePipeline):
    """Clear the reindex-required flag after verification or reingestion."""
    doc = _require_document_for_user(workflow_id, user, Permission.PIPELINE)
    old_reason = doc.get("reindex_reason")
    updated = db.mark_document_reindex_required(workflow_id, False)
    _log_audit(
        workflow_id=workflow_id,
        action_type="clear_reindex_required",
        entity_type="document",
        field_name="reindex_required",
        old_value="true",
        new_value="false",
        metadata={"reason": old_reason},
        user=user,
    )
    return {
        "workflow_id": workflow_id,
        "reindex_required": bool(updated.get("reindex_required")) if updated else False,
        "reindex_reason": updated.get("reindex_reason") if updated else None,
    }


async def _reconcile_single_document(doc: dict) -> dict:
    workflow_id = doc.get("workflow_id")
    current_stage = doc.get("stage")
    materialized = db.reconcile_materialized_state(workflow_id)
    if materialized and materialized.get("updated"):
        doc = db.get_document(workflow_id) or doc
        current_stage = doc.get("stage")
        return {
            "workflow_id": workflow_id,
            "action": "materialized_state_reconciled",
            "to": current_stage,
            "page_count": doc.get("page_count", 0),
            "chunk_count": doc.get("chunk_count", 0),
            "job_status": materialized.get("job_status"),
            "job_stage": materialized.get("job_stage"),
        }

    current_job = db.get_latest_document_job(workflow_id)
    runtime_workflow_id = (
        current_job.get("temporal_workflow_id")
        if current_job and current_job.get("status") == "running" and current_job.get("temporal_workflow_id")
        else workflow_id
    )

    try:
        handle = _api.temporal_client.get_workflow_handle(runtime_workflow_id)
        state = await asyncio.wait_for(
            handle.query("get_state"),
            timeout=5.0,
        )
        temporal_stage = state.get("stage") if state else None
        if temporal_stage and temporal_stage != current_stage:
            db.update_document_stage(workflow_id, temporal_stage)
            return {
                "workflow_id": workflow_id,
                "action": "stage_synced",
                "from": current_stage,
                "to": temporal_stage,
                "temporal_workflow_id": runtime_workflow_id,
            }
        return {
            "workflow_id": workflow_id,
            "action": "no_change",
            "stage": current_stage,
            "temporal_workflow_id": runtime_workflow_id,
        }
    except asyncio.TimeoutError:
        db.update_document_stage(workflow_id, "failed", error_message="Workflow query timed out")
        return {
            "workflow_id": workflow_id,
            "action": "marked_failed",
            "from": current_stage,
            "reason": "query_timeout",
        }
    except Exception as exc:
        error_msg = str(exc)
        if "not found" in error_msg.lower() or "workflow task" in error_msg.lower():
            db.update_document_stage(workflow_id, "failed", error_message="Workflow terminated or lost")
            db.log_audit(
                workflow_id=workflow_id,
                document_id=doc.get("document_id", ""),
                action_type="reconcile_failed",
                metadata={"from_stage": current_stage, "reason": "workflow_not_found"},
            )
            return {
                "workflow_id": workflow_id,
                "action": "marked_failed",
                "from": current_stage,
                "reason": "workflow_not_found",
            }
        return {
            "workflow_id": workflow_id,
            "action": "error",
            "from": current_stage,
            "reason": error_msg,
        }


@router.post("/documents/{workflow_id}/reconcile")
async def reconcile_single_document(workflow_id: str, user: RequirePipeline):
    """Reconcile Postgres stage with Temporal state for one document."""
    doc = _require_document_for_user(workflow_id, user, Permission.PIPELINE)
    return await _reconcile_single_document(doc)

@router.post("/documents/reconcile")
async def reconcile_document_states(user: RequirePipeline):
    """
    Reconcile Postgres document states with Temporal workflow states.

    This endpoint checks all documents in processing/review stages and updates
    Postgres if the Temporal workflow has terminated or failed. This fixes
    inconsistencies caused by external workflow termination or worker crashes.

    Returns a summary of documents checked and updated.
    """
    # Stages that indicate an active workflow (not terminal states)
    active_stages = [
        'ocr_processing', 'ocr_review',
        'translation_processing', 'translation_review',
        'chunking', 'chunk_review',
        'ready_for_ingestion', 'ingesting'
    ]

    # Scope to caller's instances (None = unrestricted bypass / all tenants).
    docs = db.list_documents(
        limit=1000,
        include_disabled=True,
        instances=_instance_scope_for_user(user, Permission.PIPELINE),
    )
    active_docs = [d for d in docs if d.get('stage') in active_stages]

    results = {
        "checked": len(active_docs),
        "updated": 0,
        "still_running": 0,
        "details": []
    }

    for doc in active_docs:
        detail = await _reconcile_single_document(doc)
        results["details"].append(detail)
        if detail.get("action") == "stage_synced" or detail.get("action") == "marked_failed":
            results["updated"] += 1
        elif detail.get("action") == "no_change":
            results["still_running"] += 1

    return results
