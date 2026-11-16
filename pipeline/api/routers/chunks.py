from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi import Path as PathParam

from pipeline import db
from pipeline.auth.deps import RequireReview, RequireSearch
from pipeline.auth.permissions import Permission
from pipeline.search.index_identity import projection_doc_id, recorded_doc_id
from pipeline.network.release_policy import index_only_mode
from pipeline.documents.models import ChunkUpdate, DocumentStage
from pipeline.api import api as _api

router = APIRouter()


@router.get("/chunks/search")
async def search_chunks_across_documents(
    user: RequireSearch,
    q: str = Query("", description="Keyword search within chunk text"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    include_excluded: bool = Query(False, description="Include excluded chunks"),
    stage: Optional[DocumentStage] = Query(None, description="Optional document stage filter"),
):
    """Search chunks across all documents for KB maintainer workflows."""
    # Tenant-scope via the owning document's instance (None = unrestricted admin).
    chunks, total = db.search_chunks(
        query=q,
        limit=limit,
        offset=offset,
        include_excluded=include_excluded,
        stage=stage.value if stage else None,
        instances=_api._instance_scope_for_user(user),
    )
    return {
        "items": chunks,
        "total": total,
        "limit": limit,
        "offset": offset,
        "query": q,
        "include_excluded": include_excluded,
        "stage": stage.value if stage else None,
    }


@router.get("/documents/{workflow_id}/chunks")
async def list_chunks(workflow_id: str, user: RequireSearch, include_excluded: bool = False):
    """Get all chunks for a document. Postgres-first for speed."""
    _api._require_document_for_user(workflow_id, user)
    return db.get_chunks(workflow_id, include_excluded=include_excluded)


@router.get("/documents/{workflow_id}/chunks/{chunk_num}")
async def get_chunk(workflow_id: str, user: RequireSearch, chunk_num: int = PathParam(..., ge=1, le=10000, description="Chunk number (1-indexed)")):
    """Get a specific chunk. Postgres-first for speed."""
    _api._require_document_for_user(workflow_id, user)
    chunk = db.get_chunk(workflow_id, chunk_num)
    if chunk:
        return chunk

    raise HTTPException(404, f"Chunk {chunk_num} not found")


@router.patch("/documents/{workflow_id}/chunks/{chunk_num}")
async def update_chunk(
    workflow_id: str,
    data: ChunkUpdate,
    user: RequireReview,
    chunk_num: int = PathParam(..., ge=1, le=10000, description="Chunk number (1-indexed)"),
):
    """Update a chunk (edit text, mark reviewed, exclude)."""
    doc = _api._require_document_for_user(workflow_id, user, Permission.REVIEW)
    old_chunk = db.get_chunk(workflow_id, chunk_num)
    if not old_chunk:
        raise HTTPException(404, f"Chunk {chunk_num} not found")

    updated = db.update_chunk(
        workflow_id,
        chunk_num,
        edited_text=data.edited_text,
        is_reviewed=data.is_reviewed,
        is_excluded=data.is_excluded,
        reviewer_notes=data.reviewer_notes,
        tags=data.tags,
    )
    if not updated:
        raise HTTPException(404, f"Chunk {chunk_num} not found")

    # Log audits for changed fields
    if data.edited_text is not None:
        old_text = old_chunk.get("edited_text") or old_chunk.get("original_text", "")
        _api._log_audit(
            workflow_id=workflow_id,
            action_type="chunk_edit",
            entity_type="chunk",
            entity_id=chunk_num,
            field_name="edited_text",
            old_value=old_text,
            new_value=data.edited_text,
        user=user,
        )

    if data.is_reviewed is not None:
        _api._log_audit(
            workflow_id=workflow_id,
            action_type="chunk_edit",
            entity_type="chunk",
            entity_id=chunk_num,
            field_name="is_reviewed",
            old_value=old_chunk.get("is_reviewed", False),
            new_value=data.is_reviewed,
        user=user,
        )

    if data.is_excluded is not None:
        _api._log_audit(
            workflow_id=workflow_id,
            action_type="chunk_edit",
            entity_type="chunk",
            entity_id=chunk_num,
            field_name="is_excluded",
            old_value=old_chunk.get("is_excluded", False),
            new_value=data.is_excluded,
        user=user,
        )

        # If excluding a chunk and document is completed (already ingested), remove from the vector index
        if data.is_excluded and not old_chunk.get("is_excluded", False):
            if index_only_mode() or doc.get("stage") == "completed":
                doc_id = projection_doc_id(doc) if index_only_mode() else recorded_doc_id(doc, "dev")
                if doc_id:
                    vector_result = _api.delete_single_chunk_from_index(doc_id, chunk_num)
                    if vector_result.get("deleted"):
                        _api._log_audit(
                            workflow_id=workflow_id,
                            action_type="chunk_removed_from_search",
                            entity_type="chunk",
                            entity_id=chunk_num,
                            metadata={"vector_id": vector_result.get("chunk_id")},
        user=user,
                        )

    if data.reviewer_notes is not None:
        _api._log_audit(
            workflow_id=workflow_id,
            action_type="chunk_edit",
            entity_type="chunk",
            entity_id=chunk_num,
            field_name="reviewer_notes",
            old_value=old_chunk.get("reviewer_notes"),
            new_value=data.reviewer_notes,
        user=user,
        )

    if data.tags is not None:
        _api._log_audit(
            workflow_id=workflow_id, action_type="chunk_edit", entity_type="chunk",
            entity_id=chunk_num, field_name="tags", old_value=old_chunk.get("tags", []),
            new_value=updated.get("tags", []), user=user,
        )

    if data.edited_text is not None or data.is_excluded is not None or data.tags is not None:
        _api._mark_reindex_required(
            workflow_id,
            "Chunk content changed; search index is out of sync",
            metadata={"chunk_number": chunk_num},
        )

    return db.get_chunk(workflow_id, chunk_num)


@router.post("/documents/{workflow_id}/chunks/{chunk_num}/reset")
async def reset_chunk(
    workflow_id: str,
    user: RequireReview,
    chunk_num: int = PathParam(..., ge=1, le=10000, description="Chunk number (1-indexed)"),
):
    """Reset chunk to original text."""
    _api._require_document_for_user(workflow_id, user, Permission.REVIEW)
    old_chunk = db.get_chunk(workflow_id, chunk_num)
    if not old_chunk:
        raise HTTPException(404, f"Chunk {chunk_num} not found")
    db.reset_chunk(workflow_id, chunk_num)

    # Log reset action
    _api._log_audit(
        workflow_id=workflow_id,
        action_type="chunk_reset",
        entity_type="chunk",
        entity_id=chunk_num,
        field_name="edited_text",
        old_value=old_chunk.get("edited_text") if old_chunk else None,
        new_value=None,
        metadata={"reset_to": "original_text"},
        user=user,
    )
    _api._mark_reindex_required(
        workflow_id,
        "Chunk reset; search index is out of sync",
        metadata={"chunk_number": chunk_num},
    )

    return db.get_chunk(workflow_id, chunk_num)
