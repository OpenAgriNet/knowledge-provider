"""Document downloads, exports, and chunk provenance routes."""

import logging
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from pipeline import db
from pipeline.api import api as _api
from pipeline.api.api import _build_provenance_links, _require_document_for_user
from pipeline.auth.deps import RequireSearch
from pipeline.network.release_policy import index_only_mode
from pipeline.object_storage import is_object_uri, parse_object_uri
from pipeline.search.index_identity import projection_doc_id
from pipeline.search.passage_projection import (
    _document_fields_from_doc,
    _prepare_records,
    _validity_fields_from_doc,
)
from pipeline.temporal.activities.indexing import _draft_provider_id

router = APIRouter()


@router.get("/documents/{workflow_id}/export/markdown")
async def export_markdown(workflow_id: str, user: RequireSearch):
    """Export document as combined markdown."""
    doc = _require_document_for_user(workflow_id, user)

    pages = db.get_pages(workflow_id)
    content = []
    for page in pages:
        md = (
            page.get("edited_translation")
            or page.get("translated_markdown")
            or page.get("edited_markdown")
            or page.get("original_markdown", "")
        )
        content.append(f"<!-- Page {page.get('page_number')} -->\n\n{md}")

    return {
        "filename": doc.get("filename", "").replace(".pdf", ".md"),
        "content": "\n\n---\n\n".join(content)
    }


@router.get("/documents/{workflow_id}/export/chunks")
async def export_chunks(workflow_id: str, user: RequireSearch, include_excluded: bool = False):
    """Export chunks as JSON for vector-index ingestion."""
    doc = _require_document_for_user(workflow_id, user)

    chunks = db.get_chunks(workflow_id, include_excluded=include_excluded)
    if include_excluded:
        chunks = [{**chunk, "is_excluded": False} for chunk in chunks]
    records = _prepare_records(
        projection_doc_id(doc), doc.get("filename", ""), chunks,
        workflow_id=workflow_id, instance=doc.get("instance"),
        pages=db.get_pages(workflow_id), source_first=index_only_mode() or doc.get("canonical_input_type") == "structured_text",
        provider_id=_draft_provider_id(doc), source_url=db.get_document_source_url(doc["id"]),
        **_document_fields_from_doc(doc), **_validity_fields_from_doc(doc),
    )
    return records


# =============================================================================
# PDF Serving
# =============================================================================

def _inline_content_disposition(filename: str) -> str:
    """Build a latin-1-safe Content-Disposition header for inline file display."""
    # Strip CR/LF/NUL so a crafted filename cannot inject response headers.
    safe_name = (filename or "document.pdf").replace('"', "'")
    safe_name = "".join(ch for ch in safe_name if ch not in "\r\n\0").strip() or "document.pdf"
    try:
        safe_name.encode("latin-1")
        return f'inline; filename="{safe_name}"'
    except UnicodeEncodeError:
        ascii_name = safe_name.encode("ascii", "ignore").decode("ascii").strip() or "document.pdf"
        encoded_name = quote(safe_name)
        return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}"


@router.get("/documents/{workflow_id}/pdf")
async def get_document_pdf(workflow_id: str, user: RequireSearch):
    """
    Get the original PDF file for a document.
    Returns the PDF as a streaming response. Postgres-first for speed.
    """
    # Postgres-first - instant lookup, tenant-scoped (404 hides other tenants).
    doc = _require_document_for_user(workflow_id, user)

    if doc.get("canonical_input_type") == "structured_text":
        raise HTTPException(404, "This document has no PDF preview")

    filepath = doc.get("filepath", "")
    filename = doc.get("filename", "document.pdf")

    if not filepath:
        raise HTTPException(404, f"Document has no PDF path: {workflow_id}")

    try:
        if is_object_uri(filepath):
            response = _api.object_store.open(parse_object_uri(filepath))

            return StreamingResponse(
                response,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": _inline_content_disposition(filename)
                }
            )
        else:
            # Local file
            file_path = Path(filepath)
            if not file_path.exists():
                raise HTTPException(404, f"PDF file not found: {filepath}")

            def file_iterator():
                with open(file_path, "rb") as f:
                    yield from f

            return StreamingResponse(
                file_iterator(),
                media_type="application/pdf",
                headers={
                    "Content-Disposition": _inline_content_disposition(filename)
                }
            )
    except HTTPException:
        raise
    except Exception as e:
        # Log the actual error server-side but don't expose details to client
        logging.error(f"PDF serving error for {workflow_id}: {str(e)}")
        raise HTTPException(500, "Error serving PDF file")


@router.get("/provenance/chunk")
async def resolve_provenance_chunk(
    request: Request,
    user: RequireSearch,
    doc_id: Optional[str] = Query(None, description="workflow slug, Postgres document_id, or legacy vector-index doc_id"),
    chunk_num: Optional[int] = Query(None, alias="chunk_num"),
):
    """
    Resolve a retrieved chunk to workflow metadata and maintainer URLs.

    Used by chat/retrieval clients when search hits lack workflow_id (legacy rows) or for enrichment.
    """
    from pipeline.documents.document_text import _infer_section

    resolved_doc_id = doc_id
    resolved_chunk_num = chunk_num

    if not resolved_doc_id or resolved_chunk_num is None:
        raise HTTPException(400, "Provide doc_id and chunk_num")

    provenance = db.resolve_chunk_provenance(doc_id=resolved_doc_id, chunk_num=int(resolved_chunk_num))
    if not provenance:
        raise HTTPException(404, "Chunk provenance not found")

    workflow_id = provenance["workflow_id"]
    # Tenant scope: a restricted caller may not resolve another tenant's chunk.
    _require_document_for_user(workflow_id, user)
    chunk = db.get_chunk(workflow_id, int(resolved_chunk_num))
    if chunk:
        text = chunk.get("edited_text") or chunk.get("original_text") or ""
        provenance["section"] = _infer_section(text, chunk.get("section_title"))
        provenance["excerpt"] = text[:320] + ("..." if len(text) > 320 else "")

    links = _build_provenance_links(workflow_id, int(resolved_chunk_num), request)
    return {**provenance, **links}
