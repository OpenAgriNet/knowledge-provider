from __future__ import annotations

from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from pipeline import db
from pipeline.documents import document_validity
from pipeline.auth.deps import CurrentUser, RequireUpload
from pipeline.auth.permissions import Permission
from pipeline.auth.tenancy import assert_document_instance_access, normalize_instance
from pipeline.network.release_policy import (
    index_only_mode,
    intake_index_options,
    managed_release_targets,
    prod_stage_disabled,
)
from pipeline.documents.models import (
    DocumentCohortsResponse,
    DocumentStage,
    DocumentSummary,
    RegisterRequest,
)
from pipeline.temporal.workflows import DocumentPipelineWorkflow, SelectNetworkRevisionWorkflow
from pipeline.api import api as _api

router = APIRouter(prefix="/documents")


class SelectRevisionRequest(BaseModel):
    revision_id: UUID
    expected_generation: UUID | None = None


def _require_managed_release_network(kind: str, network_id: str) -> None:
    try:
        targets = managed_release_targets(kind)
    except ValueError as exc:
        raise HTTPException(503, "Network release is misconfigured") from exc
    if network_id not in targets:
        raise HTTPException(404, "This deployment does not manage that network")


@router.get("/{workflow_id}/revisions")
async def list_revisions(workflow_id: str, user: CurrentUser):
    document = _api._require_document_for_user(workflow_id, user)
    result = db.list_document_revisions(document["id"])
    try:
        targets = list(managed_release_targets(document["kind"]))
    except ValueError:
        targets = []
    return {**result, "managed_network_ids": targets, "managed_network_id": targets[0] if targets else None}


@router.get("/{workflow_id}/revisions/{revision_id}")
async def read_revision(workflow_id: str, revision_id: str, user: CurrentUser):
    document = _api._require_document_for_user(workflow_id, user)
    revision = db.get_revision_snapshot(document["id"], revision_id)
    if revision is None:
        raise HTTPException(404, "Revision not found")
    snapshot = revision["snapshot"]
    return {
        "revision_id": revision["id"],
        "content_sha256": revision["content_sha256"],
        "metadata": snapshot["metadata"],
        "filename": snapshot["source"]["filename"],
        "source_url": snapshot["source"].get("url"),
        "pages": snapshot["pages"],
        "passage_count": len(snapshot["passages"]),
    }


@router.delete("/{workflow_id}/releases/{network_id}", status_code=204)
async def withdraw_release(workflow_id: str, network_id: str, revision_id: str, user: CurrentUser):
    document = _api._require_document_for_user(workflow_id, user)
    if not user.has_permission_for_instance(Permission.APPROVE_INGESTION, document["instance"]):
        raise HTTPException(403, "Network release permission is required for this provider")
    _require_managed_release_network(document["kind"], network_id)
    with db.get_connection() as conn:
        if not db.withdraw_network_release(document["id"], network_id, revision_id, _conn=conn):
            raise HTTPException(409, "The selected revision is not the current release on this network")
        _api._log_audit(
            workflow_id=workflow_id, action_type="withdraw_network_release", entity_type="document",
            metadata={"network_id": network_id, "revision_id": revision_id}, user=user, _conn=conn,
        )


@router.put("/{workflow_id}/releases/{network_id}", status_code=202)
async def select_release(workflow_id: str, network_id: str, data: SelectRevisionRequest, user: CurrentUser):
    document = _api._require_document_for_user(workflow_id, user)
    if not user.has_permission_for_instance(Permission.APPROVE_INGESTION, document["instance"]):
        raise HTTPException(403, "Network release permission is required for this provider")
    _require_managed_release_network(document["kind"], network_id)
    if document["is_disabled"] or not document["network_visible"]:
        raise HTTPException(409, "The document is unavailable for release")
    revision_id = str(data.revision_id)
    if db.get_revision_snapshot(document["id"], revision_id) is None:
        raise HTTPException(404, "Revision not found")
    release_run = f"release-{uuid4()}"
    await _api.temporal_client.start_workflow(
        SelectNetworkRevisionWorkflow.run,
        args=[document["id"], revision_id, network_id, str(data.expected_generation) if data.expected_generation else None],
        id=release_run, task_queue=_api.TASK_QUEUE,
    )
    _api._log_audit(
        workflow_id=workflow_id, action_type="select_network_revision", entity_type="document",
        metadata={"network_id": network_id, "revision_id": revision_id, "release_workflow_id": release_run}, user=user,
    )
    return {"workflow_id": release_run, "network_id": network_id, "revision_id": revision_id}


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

    The object must already be registered to the requesting provider.
    Rate limited to 10 requests/minute per IP.
    Requires permission: upload (no-op while AUTH_DISABLED=true).
    """
    create_instance = _api._resolve_create_instance(user, instance)
    filepath = _api.validate_file_path(data.filepath)
    source_filename = _api.get_filename_from_path(filepath)
    doc_kind, doc_code, replace_doc = _api._resolve_document_identity(
        user, create_instance, kind, code, source_filename
    )
    source_file_fingerprint = db.owned_source_fingerprint(filepath, create_instance)
    if not source_file_fingerprint:
        raise HTTPException(404, "Source is not registered to this provider")
    canonical_document_id = source_file_fingerprint

    workflow_id = _api.get_workflow_id(str(filepath))
    document_id = canonical_document_id

    # Reuse only when Postgres still tracks this workflow.
    # If Postgres was purged, avoid returning stale Temporal state and create a fresh run ID.
    existing_doc = db.get_document(workflow_id)
    if existing_doc and normalize_instance(existing_doc.get("instance")) == create_instance:
        # Same fingerprint/path must not leak or restart another tenant's doc.
        existing_doc = assert_document_instance_access(user, existing_doc)
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
                    instance=normalize_instance(existing_doc.get("instance")),
                    stage=DocumentStage(state.get("stage", "registered")),
                    page_count=state.get("page_count", 0),
                    chunk_count=state.get("chunk_count", 0),
                    error_message=state.get("error_message"),
                )
        except HTTPException:
            raise
        except Exception:
            pass  # Workflow doesn't exist or is not queryable; proceed to new run
    workflow_id = _api._rerun_workflow_id(workflow_id)

    index_name, auto_approve = intake_index_options(index_name, auto_approve)

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
            *([None, None, True] if index_only_mode() else []),
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
                instance=create_instance,
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
            "instance": create_instance,
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
        instance=create_instance,
        stage=DocumentStage.REGISTERED,
        page_count=0,
        chunk_count=0,
        uploaded_by_user_id=actor.get("user_id") or None,
        uploaded_by_username=actor.get("username") or None,
        uploaded_by_email=actor.get("email") or None,
        uploaded_by_roles=actor.get("roles") or [],
    )


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
    Results are limited to instances the caller can access.

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
        instances=_api._instance_scope_for_user(user),
    )

    return [_api._document_summary_from_row(doc) for doc in docs]


@router.get("/summary", response_model=DocumentCohortsResponse)
async def get_documents_summary(
    user: CurrentUser,
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled")
):
    """Return aggregate Postgres counts for dashboard totals and migration planning."""
    include_disabled = bool(x_include_disabled and x_include_disabled.lower() == "true")
    instance_scope = _api._instance_scope_for_user(user)
    summary = db.get_document_summary_counts(
        include_disabled=include_disabled,
        instances=instance_scope,
    )
    by_instance = db.get_document_counts_by_instance(
        include_disabled=include_disabled,
        instances=instance_scope,
    )
    return _api._document_cohorts_payload(summary, by_instance)


@router.get("/cohorts", response_model=DocumentCohortsResponse)
async def get_document_cohorts(
    user: CurrentUser,
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled")
):
    """Return machine-friendly cohort counts for queueing and orchestration."""
    include_disabled = bool(x_include_disabled and x_include_disabled.lower() == "true")
    instance_scope = _api._instance_scope_for_user(user)
    summary = db.get_document_summary_counts(
        include_disabled=include_disabled,
        instances=instance_scope,
    )
    by_instance = db.get_document_counts_by_instance(
        include_disabled=include_disabled,
        instances=instance_scope,
    )
    return _api._document_cohorts_payload(summary, by_instance)
