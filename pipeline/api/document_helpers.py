"""Shared document authorization, presentation, and review helpers."""

import os
import re
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

from pipeline.network.catalog import scheme_catalog
from ..auth.models import AuthUser
from ..auth.permissions import Permission
from ..auth.tenancy import allowed_instances, assert_document_instance_access, normalize_instance, resolve_create_instance
from pipeline.documents.document_kinds import validate_document_kind
from pipeline.network.release_policy import prod_stage_disabled
from pipeline.documents.models import DocumentStage, DocumentSummary
from . import api as _api


def _instance_scope_for_user(user: AuthUser, permission: Permission | None = None) -> Optional[list[str]]:
    """None = unrestricted; otherwise only these instance codes."""
    allowed = allowed_instances(user)
    if permission is None or user.is_superadmin or user.token_disabled_mode:
        return sorted(allowed) if allowed is not None else None
    # A global viewer can still have a write role in one provider.
    candidates = allowed if allowed is not None else user.instances
    return sorted(code for code in candidates if user.has_permission_for_instance(permission, code))


def _document_cohorts_payload(summary: dict, by_instance: Optional[list[dict]] = None) -> dict:
    """Shape Postgres aggregate counts for DocumentCohortsResponse (all ints)."""
    def _i(key: str, default: int = 0) -> int:
        val = summary.get(key, default)
        return int(val or 0)

    return {
        "total_documents": _i("total_documents"),
        "authoritative_documents": _i("authoritative_documents"),
        "legacy_documents": _i("legacy_documents"),
        "completed_documents": _i("completed_documents"),
        "review_queue": _i("review_queue"),
        "failed_documents": _i("failed_documents"),
        "needs_reindex": _i("needs_reindex"),
        "running_jobs": _i("running_jobs"),
        "by_stage": {
            "ocr_review": _i("ocr_review_documents"),
            "translation_review": _i("translation_review_documents"),
            "chunk_review": _i("chunk_review_documents"),
            "translation_processing": _i("translation_processing_documents"),
            "chunking": _i("chunking_documents"),
            "ready_for_ingestion": _i("ready_for_ingestion_documents"),
            "failed": _i("failed_documents"),
        },
        "by_instance": by_instance or [],
    }


def _resolve_create_instance(user: AuthUser, requested: Optional[str] = None) -> str:
    """Resolve the upload owner using the existing Keycloak tenancy rules."""
    return resolve_create_instance(user, requested)


def _document_title(filename: str) -> str:
    """Human title for a new document: the file name without extension, separators as spaces."""
    stem = Path(filename or "").stem
    return re.sub(r"[_\-\s]+", " ", stem).strip() or "Untitled document"


def _assert_can_replace(user: AuthUser, existing: dict) -> None:
    """Replacing a document's file clears its pages and chunks, so guard it.

    Needs review rights in the document's instance; a disabled document or one with
    a run in progress cannot be replaced; a document already promoted to PROD
    can only be replaced by someone who may approve ingestion.
    """
    doc_instance = existing["instance"]
    if not user.has_permission_for_instance(Permission.REVIEW, doc_instance):
        raise HTTPException(403, f"No permission to replace documents in instance: {doc_instance}")
    if existing.get("is_disabled"):
        raise HTTPException(409, "This document is disabled; restore it before replacing its file")
    latest_job = _api.db.get_latest_document_job(existing["workflow_id"])
    if latest_job and latest_job.get("status") in {"running", "start_pending"}:
        raise HTTPException(409, "A run is still in progress for this document")
    if (existing.get("index_status") or {}).get("prod") and not user.has_permission_for_instance(
        Permission.APPROVE_INGESTION, doc_instance
    ):
        raise HTTPException(
            403, "Replacing a document that is live in PROD needs approval rights in its instance"
        )


def _assert_upload_start_resolved(workflow_id: str) -> None:
    job = _api.db.get_latest_document_job(workflow_id)
    if job and job.get("status") == "start_pending":
        raise HTTPException(409, "Resolve the pending upload start first")


def _resolve_document_identity(
    user: AuthUser,
    instance: str,
    kind: str,
    code: str,
    filename: str,
) -> tuple[str, str, Optional[dict]]:
    """Kind, code and (when ``code`` already exists) the document an upload replaces.

    An explicit ``code`` that is already held in the tenant's catalog of that
    kind means "update this document": the caller reuses it instead of inserting.
    Without a code one is derived from the file name.

    Permissions are checked against the target instance, not just globally: a user
    who may upload in one state must not be able to add to, or replace a
    document of, a state where they only hold a view role. Replacing clears the
    document's pages and chunks, so it also needs review rights in that instance.
    """
    try:
        kind = validate_document_kind(kind or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not user.has_permission_for_instance(Permission.UPLOAD, instance):
        raise HTTPException(403, f"No upload permission in instance: {instance}")
    code = (code or "").strip()
    if code:
        try:
            code = scheme_catalog.validate_scheme_code(code)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        existing = _api.db.find_document_by_code(instance, kind, code)
        if existing:
            existing = assert_document_instance_access(user, existing)
            _api._assert_can_replace(user, existing)
        return kind, code, existing
    return kind, scheme_catalog.derive_scheme_code(_api._document_title(filename), kind=kind), None


def _require_document_for_user(workflow_id: str, user: AuthUser, permission: Permission | None = None) -> dict:
    """Load a document or 404 if missing / outside the caller's instance scope."""
    doc = assert_document_instance_access(user, _api.db.get_document(workflow_id))
    if permission is not None and not user.has_permission_for_instance(permission, doc.get("instance")):
        raise HTTPException(403, f"Missing provider permission: {permission.value}")
    return doc


def _document_for_user_or_none(
    workflow_id: str, user: AuthUser, permission: Permission | None = None,
) -> Optional[dict]:
    """Like _require_document_for_user but returns None instead of raising (bulk paths)."""
    try:
        return _api._require_document_for_user(workflow_id, user, permission)
    except HTTPException as exc:
        if exc.status_code not in (403, 404):
            raise
        return None


def _document_summary_from_row(doc: dict, current_job: Optional[dict] = None) -> DocumentSummary:
    return DocumentSummary(
        crop_subjects=doc.get("crop_subjects") or [],
        knowledge_metadata=doc.get("knowledge_metadata") or {},
        id=doc.get("id"),
        document_id=doc["document_id"],
        canonical_document_id=doc.get("canonical_document_id"),
        workflow_id=doc["workflow_id"],
        filename=doc["filename"],
        name=doc.get("name"),
        source_filename=doc.get("source_filename"),
        source_manifest_name=doc.get("source_manifest_name"),
        source_file_fingerprint=doc.get("source_file_fingerprint"),
        authoritative=bool(doc.get("source_manifest_name")),
        instance=normalize_instance(doc.get("instance")),
        stage=DocumentStage(doc["stage"]),
        page_count=doc.get("page_count") or 0,
        chunk_count=doc.get("chunk_count") or 0,
        error_message=doc.get("error_message"),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
        reindex_required=bool(doc.get("reindex_required")),
        reindex_reason=doc.get("reindex_reason"),
        available_actions=_api._list_available_actions(
            doc,
            current_job if current_job is not None else _api.db.get_latest_document_job(doc["workflow_id"]),
        ),
        uploaded_by_user_id=doc.get("uploaded_by_user_id"),
        uploaded_by_username=doc.get("uploaded_by_username"),
        uploaded_by_email=doc.get("uploaded_by_email"),
        uploaded_by_roles=_api._parse_roles_field(doc.get("uploaded_by_roles")),
        kind=doc.get("kind"),
        code=doc.get("code"),
        aliases=list(doc.get("aliases") or []),
        tool_routing=doc.get("tool_routing"),
        catalog_visible=bool(int(doc["catalog_visible"])) if doc.get("catalog_visible") is not None else True,
        network_visible=bool(int(doc["network_visible"])) if doc.get("network_visible") is not None else True,
        prod_ready_requested_at=doc.get("prod_ready_requested_at"),
        prod_ready_requested_by_username=doc.get("prod_ready_requested_by_username"),
        valid_from=doc.get("valid_from"),
        valid_to=doc.get("valid_to"),
    )


def _provenance_base_urls(request: Request) -> tuple[str, str]:
    api_base = (os.environ.get("DOCS_PIPELINE_API_URL") or str(request.base_url)).rstrip("/")
    ui_base = (os.environ.get("DOCS_PIPELINE_UI_URL") or "http://localhost:3000").rstrip("/")
    return api_base, ui_base


def _build_provenance_links(workflow_id: str, chunk_num: int, request: Request) -> dict[str, str]:
    api_base, ui_base = _api._provenance_base_urls(request)
    return {
        "pdf_url": f"{api_base}/documents/{workflow_id}/pdf",
        "document_url": f"{ui_base}/documents/{workflow_id}",
        "chunk_url": f"{ui_base}/documents/{workflow_id}?tab=chunks&chunk={chunk_num}",
    }


def _list_available_actions(doc: dict, current_job: Optional[dict] = None) -> list[str]:
    if not doc:
        return []
    if doc.get("is_disabled"):
        return ["restore_document"]

    if current_job and current_job.get("status") == "start_pending":
        return ["retry_upload_start"]

    stage = doc.get("stage")
    actions = ["disable_document", "reconcile_document"]
    automatic_release = doc.get("uses_network_releases", False)
    if stage == "ocr_review" and not automatic_release:
        actions.append("approve_ocr")
    elif stage == "translation_review" and not automatic_release:
        actions.append("approve_translation")
    elif stage == "chunk_review" and not automatic_release:
        actions.append("approve_chunks")
    elif stage == "ready_for_ingestion" and not automatic_release:
        actions.append("approve_ingestion")
    elif stage == "approval_for_prod":
        # A document already parked at this gate when DISABLE_PROD_SETTING was
        # turned on keeps its approve action — otherwise it would be stranded
        # with no way forward.
        actions.append("approve_prod")
        if not doc.get("prod_ready_requested_at"):
            actions.append("request_prod_ready")
    elif stage == "completed":
        actions.append("reingest_document")
        if not automatic_release and not prod_stage_disabled():
            actions.append("approve_prod")
    elif stage == "failed":
        if not doc.get("ocr_completed_at"):
            actions.append("retry_ocr")
        if doc.get("ocr_completed_at") and not doc.get("translation_completed_at"):
            actions.append("retry_translation")
        if doc.get("translation_completed_at"):
            actions.append("retry_chunking")

    if doc.get("reindex_required"):
        actions.extend(["reingest_document", "clear_reindex_required"])
    else:
        actions.append("mark_reindex_required")

    if current_job and current_job.get("status") == "running":
        actions.append("inspect_runtime")

    return sorted(set(actions))


def _mark_reindex_required(workflow_id: str, reason: str, metadata: Optional[dict] = None) -> Optional[dict]:
    doc = _api.db.mark_document_reindex_required(workflow_id, True, reason)
    if doc:
        _api.db.log_audit(
            workflow_id=workflow_id,
            document_id=doc.get("document_id", workflow_id),
            action_type="mark_reindex_required",
            field_name="reindex_required",
            old_value="false",
            new_value="true",
            metadata={"reason": reason, **(metadata or {})},
        )
    return doc
