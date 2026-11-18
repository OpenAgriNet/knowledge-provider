from fastapi import APIRouter, HTTPException
from fastapi import Path as PathParam

from pipeline import db
from pipeline.api import api as _api
from pipeline.auth.deps import RequireReview, RequireSearch
from pipeline.auth.permissions import Permission
from pipeline.documents.models import PageUpdate

router = APIRouter()


@router.get("/documents/{workflow_id}/pages")
async def list_pages(workflow_id: str, user: RequireSearch):
    """Get all pages for a document. Postgres-first for speed."""
    # Enforce tenant scope up front (404 hides other tenants' documents).
    _api._require_document_for_user(workflow_id, user)
    return db.get_pages(workflow_id)


@router.get("/documents/{workflow_id}/pages/{page_num}")
async def get_page(workflow_id: str, user: RequireSearch, page_num: int = PathParam(..., ge=1, le=10000, description="Page number (1-indexed)")):
    """Get a specific page. Postgres-first for speed."""
    _api._require_document_for_user(workflow_id, user)
    page = db.get_page(workflow_id, page_num)
    if page:
        return page

    raise HTTPException(404, f"Page {page_num} not found")


@router.patch("/documents/{workflow_id}/pages/{page_num}")
async def update_page(
    workflow_id: str,
    data: PageUpdate,
    user: RequireReview,
    page_num: int = PathParam(..., ge=1, le=10000, description="Page number (1-indexed)"),
):
    """Update a page (edit markdown, mark reviewed)."""
    doc = _api._require_document_for_user(workflow_id, user, Permission.REVIEW)
    old_page = db.get_page(workflow_id, page_num)
    if not old_page:
        raise HTTPException(404, f"Page {page_num} not found")

    try:
        updated = db.update_page(
            workflow_id,
            page_num,
            edited_markdown=data.edited_markdown,
            is_reviewed=data.is_reviewed,
            reviewer_notes=data.reviewer_notes,
            edited_translation=data.edited_translation,
            translation_reviewed=data.translation_reviewed,
            translation_notes=data.translation_notes,
            translation_language=data.translation_language,
        )
    except db.PageTranslationMissing as exc:
        raise HTTPException(409, "This translation language is unavailable or ambiguous. Reload the page.") from exc
    if not updated:
        raise HTTPException(404, f"Page {page_num} not found")

    # Log audits for changed fields
    if data.edited_markdown is not None:
        old_text = old_page.get("edited_markdown") or old_page.get("original_markdown", "")
        _api._log_audit(
            workflow_id=workflow_id,
            action_type="page_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="edited_markdown",
            old_value=old_text,
            new_value=data.edited_markdown,
        user=user,
        )

    if data.is_reviewed is not None:
        _api._log_audit(
            workflow_id=workflow_id,
            action_type="page_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="is_reviewed",
            old_value=old_page.get("is_reviewed", False),
            new_value=data.is_reviewed,
        user=user,
        )

    if data.reviewer_notes is not None:
        _api._log_audit(
            workflow_id=workflow_id,
            action_type="page_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="reviewer_notes",
            old_value=old_page.get("reviewer_notes"),
            new_value=data.reviewer_notes,
        user=user,
        )

    translation_language = data.translation_language or old_page.get("translation_target_language")
    translations = old_page.get("translations") or {}
    translation_key = (
        db.page_translation_key(translations, translation_language) if data.translation_language else translation_language
    )
    translation = translations.get(translation_key, {})
    for field, old_value, new_value in (
        ("edited_translation", translation.get("edited_translation") or translation.get("translated_markdown"), data.edited_translation),
        ("translation_reviewed", bool(translation.get("is_reviewed")), data.translation_reviewed),
        ("translation_notes", translation.get("reviewer_notes"), data.translation_notes),
    ):
        if new_value is not None:
            _api._log_audit(
                workflow_id=workflow_id,
                action_type="translation_edit",
                entity_type="page",
                entity_id=page_num,
                field_name=field,
                old_value=old_value,
                new_value=new_value,
                metadata={"language": translation_language} if translation_language else None,
                user=user,
            )

    # Review flags/notes alone must not dirty the search index — only content edits.
    content_changed = data.edited_markdown is not None or data.edited_translation is not None
    if doc and content_changed and (
        doc.get("chunk_count", 0) > 0
        or doc.get("stage") in {"chunking", "chunk_review", "ready_for_ingestion", "ingesting", "approval_for_prod", "ingesting_prod", "completed"}
    ):
        _api._mark_reindex_required(
            workflow_id,
            "Page content changed after chunk generation; rechunk and reindex required",
            metadata={"page_number": page_num},
        )

    return db.get_page(workflow_id, page_num)


@router.post("/documents/{workflow_id}/pages/{page_num}/reset")
async def reset_page(
    workflow_id: str,
    user: RequireReview,
    page_num: int = PathParam(..., ge=1, le=10000, description="Page number (1-indexed)"),
):
    """Reset page to original OCR output."""
    doc = _api._require_document_for_user(workflow_id, user, Permission.REVIEW)
    old_page = db.get_page(workflow_id, page_num)
    if not old_page:
        raise HTTPException(404, f"Page {page_num} not found")
    db.reset_page(workflow_id, page_num)

    # Log reset action
    _api._log_audit(
        workflow_id=workflow_id,
        action_type="page_reset",
        entity_type="page",
        entity_id=page_num,
        field_name="edited_markdown",
        old_value=old_page.get("edited_markdown") if old_page else None,
        new_value=None,
        metadata={"reset_to": "original_markdown"},
        user=user,
    )

    if doc and (doc.get("chunk_count", 0) > 0 or doc.get("stage") in {"chunking", "chunk_review", "ready_for_ingestion", "ingesting", "approval_for_prod", "ingesting_prod", "completed"}):
        _api._mark_reindex_required(
            workflow_id,
            "Page reset after chunk generation; rechunk and reindex required",
            metadata={"page_number": page_num},
        )

    return db.get_page(workflow_id, page_num)
