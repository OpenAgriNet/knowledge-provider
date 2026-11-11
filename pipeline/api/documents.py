from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request

from .. import db, document_validity
from ..auth.deps import CurrentUser, RequireUpload
from ..auth.tenancy import assert_document_group_access, normalize_group
from ..group_names import prod_stage_disabled
from ..models import (
    DocumentCohortsResponse,
    DocumentStage,
    DocumentSummary,
    RegisterFolderRequest,
    RegisterRequest,
)
from ..workflows import DocumentPipelineWorkflow
from . import api as _api

router = APIRouter(prefix="/documents")


@router.post("", response_model=DocumentSummary)
@_api.limiter.limit(_api.RATE_LIMIT_UPLOAD)
async def start_document_workflow(
    request: Request,  # Required for rate limiting
    data: RegisterRequest,
    user: RequireUpload,
    auto_approve: bool = False,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
    index_name: str = "documents-index",
    stop_after_ocr: bool = False,
    instance: str = "",
    kind: str = Query(..., description="scheme or advisory"),
    code: str = "",
):
    """
    Start a new document processing workflow.

    The workflow will:
    1. Run OCR
    2. Wait for approval (unless auto_approve=True)
    3. Create chunks
    4. Wait for approval (unless auto_approve=True)
    5. Ingest to the vector index

    Note: File path must be within allowed directories (ALLOWED_FILE_PATHS env var).
    Rate limited to 10 requests/minute per IP.
    Requires permission: upload (no-op while AUTH_DISABLED=true).
    """
    create_group = _api._resolve_upload_instance(user, instance)
    # Validate file path to prevent path traversal attacks
    filepath = _api.validate_file_path(data.filepath)
    source_filename = _api.get_filename_from_path(filepath)
    doc_kind, doc_code, replace_doc = _api._resolve_document_identity(
        user, create_group, kind, code, source_filename
    )
    source_file_fingerprint = _api._compute_file_fingerprint(filepath)
    canonical_document_id = source_file_fingerprint

    workflow_id = _api.get_workflow_id(str(filepath))
    document_id = canonical_document_id

    # Reuse only when Postgres still tracks this workflow.
    # If Postgres was purged, avoid returning stale Temporal state and create a fresh run ID.
    existing_doc = db.get_document(workflow_id)
    if existing_doc:
        # Same fingerprint/path must not leak or restart another tenant's doc.
        existing_doc = assert_document_group_access(user, existing_doc)
        try:
            handle = _api.temporal_client.get_workflow_handle(workflow_id)
            state = await handle.query("get_state")
            if state:
                return DocumentSummary(
                    document_id=document_id,
                    canonical_document_id=canonical_document_id,
                    workflow_id=workflow_id,
                    filename=source_filename,
                    source_filename=source_filename,
                    source_file_fingerprint=source_file_fingerprint,
                    authoritative=bool(existing_doc.get("source_manifest_name")) if existing_doc else False,
                    instance=normalize_group(existing_doc.get("group")),
                    stage=DocumentStage(state.get("stage", "registered")),
                    page_count=state.get("page_count", 0),
                    chunk_count=state.get("chunk_count", 0),
                    error_message=state.get("error_message"),
                )
        except HTTPException:
            raise
        except Exception:
            pass  # Workflow doesn't exist or is not queryable; proceed to new run
    else:
        workflow_id = _api._rerun_workflow_id(workflow_id)

    # Start new workflow
    handle = await _api.temporal_client.start_workflow(
        DocumentPipelineWorkflow.run,
        args=[
            document_id,
            _api.get_filename_from_path(filepath),
            str(filepath),
            chunk_size,
            chunk_overlap,
            min_tokens,
            index_name,
            auto_approve,
            stop_after_ocr,
            prod_stage_disabled(),
        ],
        id=workflow_id,
        task_queue=_api.TASK_QUEUE,
    )

    # Save to Postgres for visibility during processing
    actor = _api._actor_from_user(user)
    # The period this document is searchable in starts here, at the one moment
    # that knows the upload day. A reviewer confirms or moves it later, on the
    # form that sets the document type.
    validity = document_validity.default_period()
    if replace_doc:
        document_uuid = replace_doc["id"]
        db.reset_document_for_reupload(
            document_uuid,
            filename=source_filename,
            filepath=str(filepath),
            fingerprint=source_file_fingerprint,
        )
    else:
        try:
            document_uuid = db.upsert_document(
                workflow_id=workflow_id,
                document_id=document_id,
                canonical_document_id=canonical_document_id,
                filename=source_filename,
                source_filename=source_filename,
                source_file_fingerprint=source_file_fingerprint,
                filepath=str(filepath),
                stage="registered",
                stop_after_ocr=stop_after_ocr,
                group=create_group,
                uploaded_by_user_id=actor.get("user_id") or None,
                uploaded_by_username=actor.get("username") or None,
                uploaded_by_email=actor.get("email") or None,
                uploaded_by_roles=actor.get("roles_csv") or None,
                valid_from=validity.start_date,
                valid_to=validity.end_date,
                with_workflow=False,
                kind=doc_kind,
                code=doc_code,
                name=_api._document_title(source_filename),
            )
        except db.DuplicateDocumentCode as exc:
            raise HTTPException(409, str(exc)) from exc
    db.create_document_job(
        workflow_id=document_uuid,
        job_type="ocr_only" if stop_after_ocr else "pipeline",
        temporal_workflow_id=workflow_id,
        status="running",
        current_stage="registered",
        config={
            "auto_approve": auto_approve,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "min_tokens": min_tokens,
            "index_name": index_name,
            "stop_after_ocr": stop_after_ocr,
            "uploaded_by": actor,
        },
        actor=actor,
    )
    _api._log_audit(
        workflow_id=workflow_id,
        action_type="document_upload",
        entity_type="document",
        field_name="registered",
        new_value=source_filename,
        metadata={
            "source": "register_endpoint",
            "instance": create_group,
            "kind": doc_kind,
            "code": doc_code,
            "replaced_existing": bool(replace_doc),
        },
        user=user,
    )

    return DocumentSummary(
        document_id=document_id,
        canonical_document_id=canonical_document_id,
        workflow_id=workflow_id,
        filename=source_filename,
        source_filename=source_filename,
        source_file_fingerprint=source_file_fingerprint,
        authoritative=False,
        instance=create_group,
        stage=DocumentStage.REGISTERED,
        page_count=0,
        chunk_count=0,
        uploaded_by_user_id=actor.get("user_id") or None,
        uploaded_by_username=actor.get("username") or None,
        uploaded_by_email=actor.get("email") or None,
        uploaded_by_roles=actor.get("roles") or [],
    )


@router.post("/batch", response_model=list[DocumentSummary])
@_api.limiter.limit("5/minute")  # Stricter limit for batch operations
async def start_batch_workflows(
    request: Request,  # Required for rate limiting
    data: RegisterFolderRequest,
    user: RequireUpload,
    auto_approve: bool = False,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
    stop_after_ocr: bool = False,
    instance: str = "",
    kind: str = Query(..., description="scheme or advisory"),
):
    """Start workflows for all supported documents in a directory."""
    create_group = _api._resolve_upload_instance(user, instance)
    directory = Path(data.directory)
    if not directory.exists():
        raise HTTPException(404, f"Directory not found: {data.directory}")

    candidate_files = [p for p in directory.glob("*") if p.is_file() and p.suffix.lower() in _api.ALLOWED_EXTENSIONS]
    if not candidate_files:
        raise HTTPException(400, "No supported files found")

    results = []
    for pdf_path in candidate_files:
        try:
            result = await start_document_workflow(
                request,
                RegisterRequest(filepath=str(pdf_path)),
                user,
                auto_approve=auto_approve,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                min_tokens=min_tokens,
                stop_after_ocr=stop_after_ocr,
                instance=create_group,
                kind=kind,
                code="",
            )
            results.append(result)
        except Exception as e:
            # Log full error, return sanitized message
            logging.error(f"Batch workflow error for {pdf_path.name}: {str(e)}")
            results.append(DocumentSummary(
                document_id=hashlib.md5(str(pdf_path).encode()).hexdigest(),
                workflow_id=_api.get_workflow_id(str(pdf_path)),
                filename=pdf_path.name,
                authoritative=False,
                stage=DocumentStage.FAILED,
                page_count=0,
                chunk_count=0,
                error_message="Failed to start workflow",
            ))

    return results


@router.get("", response_model=list[DocumentSummary])
async def list_documents(
    user: CurrentUser,
    stage: Optional[DocumentStage] = None,
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled")
):
    """
    List all document workflows.

    Uses Postgres for fast listing (no Temporal queries for performance).
    Disabled (soft-deleted) documents are excluded by default - use X-Include-Disabled: true to show them.
    Results are limited to groups the caller can access.

    Pagination:
    - limit: Max documents to return (default 100, max 500)
    - offset: Skip first N documents (default 0)
    """
    stage_filter = stage.value if stage else None
    include_disabled = bool(x_include_disabled and x_include_disabled.lower() == "true")

    # Use Postgres only for fast listing - no Temporal queries
    docs = db.list_documents(
        stage=stage_filter,
        limit=limit,
        offset=offset,
        include_disabled=include_disabled,
        groups=_api._group_scope_for_user(user),
    )

    return [_api._document_summary_from_row(doc) for doc in docs]


@router.get("/summary", response_model=DocumentCohortsResponse)
async def get_documents_summary(
    user: CurrentUser,
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled")
):
    """Return aggregate Postgres counts for dashboard totals and migration planning."""
    include_disabled = bool(x_include_disabled and x_include_disabled.lower() == "true")
    group_scope = _api._group_scope_for_user(user)
    summary = db.get_document_summary_counts(
        include_disabled=include_disabled,
        groups=group_scope,
    )
    by_group = db.get_document_counts_by_group(
        include_disabled=include_disabled,
        groups=group_scope,
    )
    return _api._document_cohorts_payload(summary, by_group)


@router.get("/cohorts", response_model=DocumentCohortsResponse)
async def get_document_cohorts(
    user: CurrentUser,
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled")
):
    """Return machine-friendly cohort counts for queueing and orchestration."""
    include_disabled = bool(x_include_disabled and x_include_disabled.lower() == "true")
    group_scope = _api._group_scope_for_user(user)
    summary = db.get_document_summary_counts(
        include_disabled=include_disabled,
        groups=group_scope,
    )
    by_group = db.get_document_counts_by_group(
        include_disabled=include_disabled,
        groups=group_scope,
    )
    return _api._document_cohorts_payload(summary, by_group)
