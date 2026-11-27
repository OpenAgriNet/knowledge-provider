"""Document and platform index inspection routes."""

from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

from pipeline import db
from pipeline.api import api as _api
from pipeline.api.api import _instance_scope_for_user, _require_document_for_user
from pipeline.auth.deps import RequireAdmin, RequireSearch
from pipeline.auth.tenancy import unrestricted
from pipeline.network.release_policy import index_only_mode
from pipeline.search.index_identity import projection_doc_id, projection_owner

router = APIRouter()


@router.get("/documents/{workflow_id}/qdrant")
async def get_document_qdrant_status(
    workflow_id: str,
    user: RequireSearch,
    index_name: str | None = Query(None),
):
    """Return vector-index status/chunks for a document."""
    return await _document_qdrant_status(workflow_id, user, index_name)


async def _document_qdrant_status(workflow_id, user, index_name, *, chunk_number=None):
    from pipeline.vector_store import get_default_index_name, get_vector_store

    doc = _require_document_for_user(workflow_id, user)

    entries = doc.get("index_status") or {}
    default_index = get_default_index_name()
    if index_name:
        resolved_index = default_index if index_name == "documents-index" else index_name
    elif index_only_mode():
        resolved_index = default_index
    else:
        preferred = entries.get(default_index) or next(iter(entries.values()), {})
        resolved_index = preferred.get("index_name") or default_index
    matching = next((entry for entry in entries.values() if entry.get("index_name") == resolved_index), None)
    if not matching and resolved_index != default_index:
        raise HTTPException(404, "Index not found for this document")
    index_doc_id = (matching or {}).get("doc_id") or projection_doc_id(doc)
    store = get_vector_store()
    backend = store.backend

    try:
        if chunk_number is None:
            raw_hits = store.list_by_doc_id(resolved_index, index_doc_id, limit=1000)
        else:
            raw_hits = store.list_by_doc_id(
                resolved_index, index_doc_id, chunk_num=chunk_number, raw_payload=True,
            )
    except Exception as exc:
        _api.logger.warning("Document index diagnostics unavailable backend=%s error=%s", backend, type(exc).__name__)
        raise HTTPException(
            503,
            "Vector diagnostics are unavailable",
        ) from exc

    hits = []
    for hit in raw_hits:
        hit_doc_id = hit.get("doc_id") or ""
        if hit_doc_id != index_doc_id or hit.get("instance") != doc.get("instance"):
            continue
        if projection_owner(hit_doc_id) != str(doc["id"]) and hit.get("workflow_id") != doc["workflow_id"]:
            continue
        if chunk_number is not None:
            hits.append(hit)
            continue
        normalized_hit = dict(hit) if user.is_superadmin else {
            key: hit[key] for key in ("_id", "chunk_num", "text") if key in hit
        }
        chunk_num = normalized_hit.get("chunk_num")
        normalized_hit.setdefault(
            "_id",
            f"{normalized_hit.get('doc_id', index_doc_id)}:{chunk_num if chunk_num is not None else 'unknown'}",
        )
        normalized_hit.setdefault("chunk_number", chunk_num)
        hits.append(normalized_hit)
    if chunk_number is not None:
        return {"hits": hits}
    db_chunks = db.get_chunks(workflow_id, include_excluded=True)
    status = {
        "workflow_id": workflow_id,
        "index_name": resolved_index,
        "backend": backend,
        "index_doc_id": index_doc_id,
        "doc_id": index_doc_id,
        "sqlite_chunk_count": len([c for c in db_chunks if not c.get("is_excluded")]),
        "indexed_chunk_count": len(hits),
        "status": "indexed" if hits else "missing",
        "hits": hits,
    }
    return status


@router.get("/documents/{workflow_id}/qdrant/chunks")
async def list_document_qdrant_chunks(
    workflow_id: str,
    user: RequireSearch,
    index_name: str | None = Query(None),
    chunk_number: int | None = Query(None, ge=1),
):
    if not user.is_superadmin:
        raise HTTPException(403, "Raw index diagnostics require super admin access")
    result = await _document_qdrant_status(
        workflow_id, user, index_name=index_name, chunk_number=chunk_number,
    )
    return result["hits"]


@router.get("/indexes/{index_name}/settings")
async def get_vector_index_settings(index_name: str, user: RequireSearch):
    from pipeline.vector_store import get_vector_store

    if not unrestricted(user):
        raise HTTPException(403, "Global index inspection requires platform access")
    try:
        return get_vector_store().get_settings(index_name)
    except Exception as exc:
        raise HTTPException(404, f"Index '{index_name}' settings unavailable: {exc}") from exc


@router.get("/indexes/{index_name}/stats")
async def get_vector_index_stats(index_name: str, user: RequireSearch):
    from pipeline.vector_store import get_vector_store

    if not unrestricted(user):
        raise HTTPException(403, "Global index inspection requires platform access")
    try:
        return get_vector_store().get_stats(index_name)
    except Exception as exc:
        raise HTTPException(404, f"Index '{index_name}' stats unavailable: {exc}") from exc


@router.get("/indexes/summary")
async def get_vector_indexes_summary(
    user: RequireSearch,
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled"),
):
    """Summarize index coverage from Postgres-backed index status plus live vector-store stats."""
    from pipeline.vector_store import get_vector_store

    include_disabled = x_include_disabled and x_include_disabled.lower() == "true"
    scope = _instance_scope_for_user(user)
    summaries = db.list_index_summaries(
        include_disabled=include_disabled,
        instances=scope,
    )
    if not summaries:
        return []

    store = get_vector_store()
    backend = store.backend

    results = []
    for summary in summaries:
        live_stats = None
        live_error = None
        if scope is None:
            try:
                live_stats = store.get_stats(summary["index_name"])
            except Exception as exc:
                _api.logger.warning("Index summary unavailable error=%s", type(exc).__name__)
                live_error = "Vector index statistics are unavailable"
        results.append({
            **summary,
            "backend": backend,
            "live_stats": live_stats,
            "live_error": live_error,
        })
    return results

@router.get("/admin/index/schema")
async def get_vector_index_schema(
    user: RequireAdmin,
    index_name: str = Query("documents-index", description="Vector index / collection name"),
):
    """Report the live vector index's field schema vs. the canonical passage schema."""
    from pipeline.search.passage_projection import _passage_schema_field_names
    from pipeline.vector_store import get_default_index_name, get_vector_store

    resolved = index_name or get_default_index_name()
    store = get_vector_store()
    try:
        index_settings = store.get_settings(resolved)
    except Exception as exc:
        raise HTTPException(404, f"Index '{resolved}' not found: {exc}") from exc

    field_names = sorted(
        f.get("name")
        for f in (index_settings.get("allFields") or [])
        if isinstance(f, dict) and f.get("name")
    )
    canonical_fields = sorted(_passage_schema_field_names())
    missing_fields = sorted(set(canonical_fields) - set(field_names))

    return {
        "index_name": resolved,
        "backend": store.backend,
        "fields": field_names,
        "canonical_passage_fields": canonical_fields,
        "missing_canonical_fields": missing_fields,
    }


@router.post("/admin/index/create")
async def create_vector_index(
    user: RequireAdmin,
    index_name: str = Query("documents-index", description="Vector index / collection name"),
    recreate_if_exists: bool = Query(False, description="If true, delete existing index and create with passage schema"),
):
    """Create the vector index/collection — a cosine Qdrant collection sized from EMBEDDING_VECTOR_SIZE."""
    _ = user
    from pipeline.network.release_policy import index_only_mode
    from pipeline.vector_store import get_default_index_name, get_vector_store

    resolved = get_default_index_name() if index_only_mode() else index_name or get_default_index_name()
    store = get_vector_store()
    result = store.ensure_collection(resolved, recreate=recreate_if_exists)
    return {
        "index_name": resolved,
        "backend": store.backend,
        **result,
    }


@router.get("/admin/ingest-info")
async def get_ingest_info(user: RequireAdmin):
    """
    Return what the running container's ingest code would send to the vector index.
    Use this to verify the API/worker image has the passage schema (text_for_embedding, etc.).
    """
    from pipeline.search.passage_projection import _passage_schema_field_names, _prepare_records

    passage_fields = sorted(_passage_schema_field_names())
    has_text_for_embedding = "text_for_embedding" in set(passage_fields)
    # One fake chunk to see exact record shape the worker would send
    fake_chunk = {
        "chunk_number": 0,
        "original_text": "Sample text.",
        "edited_text": None,
        "is_excluded": False,
        "token_count": 2,
        "page_start": 1,
        "page_end": 1,
    }
    sample_records = _prepare_records(
        document_id="debug-document-id",
        filename="debug.pdf",
        chunks=[fake_chunk],
        workflow_id="doc-debugsample12",
    )
    sample_record_keys = sorted(sample_records[0].keys()) if sample_records else []
    return {
        "passage_schema_fields": passage_fields,
        "has_text_for_embedding": has_text_for_embedding,
        "sample_record_keys": sample_record_keys,
        "sample_has_passage_prefix": (
            sample_records[0].get("text_for_embedding", "").startswith("passage:")
            if sample_records else False
        ),
    }
