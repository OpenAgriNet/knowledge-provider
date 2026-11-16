"""Document review and legacy approval routes."""

from fastapi import APIRouter, HTTPException

from pipeline import db
from pipeline.auth.deps import RequireAdmin, RequireApproveIngestion, RequirePipeline, RequireReview
from pipeline.auth.models import AuthUser
from pipeline.auth.permissions import Permission
from pipeline.network.release_policy import index_only_mode, prod_stage_disabled
from pipeline.documents.models import (
    BulkWorkflowActionRequest,
    BulkWorkflowActionResponse,
    BulkWorkflowActionResult,
)
from pipeline.temporal.workflows import DocumentPipelineWorkflow, PromoteToProdWorkflow
from pipeline.api import api as _api
from pipeline.api.api import (
    _actor_from_user,
    _document_for_user_or_none,
    _log_audit,
    _require_document_for_user,
)
from pipeline.api.routers.document_lifecycle import reingest_document

router = APIRouter()


_STAGE_APPROVAL_HINTS = {
    "ocr_review": "POST /documents/{id}/approve-ocr",
    "translation_review": "POST /documents/{id}/approve-translation",
    "chunk_review": "POST /documents/{id}/approve-chunks",
    "ready_for_ingestion": "POST /documents/{id}/approve-ingestion",
    "approval_for_prod": "POST /documents/{id}/approve-prod",
}


async def _validate_approval_stage(workflow_id: str, expected_stage: str):
    """Validate that workflow is in the expected stage before approval."""
    try:
        handle = _api.temporal_client.get_workflow_handle(workflow_id)
        state = await handle.query("get_state")
        current_stage = state.get("stage") if isinstance(state, dict) else getattr(state, "stage", None)
        if current_stage != expected_stage:
            hint = _STAGE_APPROVAL_HINTS.get(str(current_stage or ""))
            extra = f" Use {hint.replace('{id}', workflow_id)} instead." if hint else ""
            raise HTTPException(
                400,
                (
                    f"Cannot approve: workflow is in '{current_stage}' stage, "
                    f"expected '{expected_stage}'.{extra}"
                ),
            )
        return handle
    except HTTPException:
        raise
    except Exception:
        # Try Postgres fallback to check if workflow exists but is completed/failed
        doc = db.get_document(workflow_id)
        if doc:
            current = doc.get("stage")
            hint = _STAGE_APPROVAL_HINTS.get(str(current or ""))
            extra = f" Use {hint.replace('{id}', workflow_id)} instead." if hint else ""
            raise HTTPException(
                400,
                (
                    f"Cannot approve: workflow is in '{current}' stage "
                    f"(completed/failed workflows cannot be approved).{extra}"
                ),
            )
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


async def _execute_bulk_approval_action(
    request: BulkWorkflowActionRequest,
    action: str,
    expected_stage: str,
    signal_method,
    user: AuthUser,
) -> BulkWorkflowActionResponse:
    results: list[BulkWorkflowActionResult] = []
    for workflow_id in request.workflow_ids:
        doc = _document_for_user_or_none(workflow_id, user, Permission.REVIEW)
        if not doc:
            results.append(BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=False,
                action=action,
                message="document_not_found",
            ))
            continue
        current_stage = doc.get("stage")
        if current_stage != expected_stage:
            results.append(BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=False,
                action=action,
                message=f"invalid_stage:{current_stage}",
            ))
            continue
        if request.dry_run:
            results.append(BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=True,
                action=action,
                message="would_execute",
            ))
            continue
        try:
            handle = await _validate_approval_stage(workflow_id, expected_stage)
            await handle.signal(signal_method)
            results.append(BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=True,
                action=action,
                message="queued",
            ))
        except Exception as exc:
            results.append(BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=False,
                action=action,
                message=str(exc),
            ))

    return BulkWorkflowActionResponse(
        action=action,
        dry_run=request.dry_run,
        requested=len(request.workflow_ids),
        succeeded=sum(1 for result in results if result.ok),
        failed=sum(1 for result in results if not result.ok),
        results=results,
    )


@router.post("/documents/bulk/approve-ocr", response_model=BulkWorkflowActionResponse)
async def bulk_approve_ocr(request: BulkWorkflowActionRequest, user: RequireReview):
    """Bulk-approve documents waiting in OCR review."""
    return await _execute_bulk_approval_action(
        request,
        action="approve_ocr",
        expected_stage="ocr_review",
        signal_method=DocumentPipelineWorkflow.approve_ocr,
        user=user,
    )


@router.post("/documents/bulk/approve-translation", response_model=BulkWorkflowActionResponse)
async def bulk_approve_translation(request: BulkWorkflowActionRequest, user: RequireReview):
    """Bulk-approve documents waiting in translation review."""
    return await _execute_bulk_approval_action(
        request,
        action="approve_translation",
        expected_stage="translation_review",
        signal_method=DocumentPipelineWorkflow.approve_translation,
        user=user,
    )


@router.post("/documents/bulk/approve-chunks", response_model=BulkWorkflowActionResponse)
async def bulk_approve_chunks(request: BulkWorkflowActionRequest, user: RequireReview):
    """Bulk-approve documents waiting in chunk review."""
    return await _execute_bulk_approval_action(
        request,
        action="approve_chunks",
        expected_stage="chunk_review",
        signal_method=DocumentPipelineWorkflow.approve_chunks,
        user=user,
    )


@router.post("/documents/bulk/reindex", response_model=BulkWorkflowActionResponse)
async def bulk_reindex_documents(
    request: BulkWorkflowActionRequest,
    user: RequirePipeline,
    index_name: str = "documents-index",
):
    """Bulk queue reingestion for completed or dirty documents."""
    results: list[BulkWorkflowActionResult] = []
    for workflow_id in request.workflow_ids:
        doc = _document_for_user_or_none(workflow_id, user, Permission.PIPELINE)
        if not doc:
            results.append(BulkWorkflowActionResult(workflow_id=workflow_id, ok=False, action="reindex", message="document_not_found"))
            continue
        if doc.get("stage") not in {"completed", "ready_for_ingestion", "chunk_review"} and not doc.get("reindex_required"):
            results.append(BulkWorkflowActionResult(workflow_id=workflow_id, ok=False, action="reindex", message=f"invalid_stage:{doc.get('stage')}"))
            continue
        if request.dry_run:
            results.append(BulkWorkflowActionResult(workflow_id=workflow_id, ok=True, action="reindex", message="would_execute"))
            continue
        try:
            await reingest_document(workflow_id, user=user, index_name=index_name)
            results.append(BulkWorkflowActionResult(workflow_id=workflow_id, ok=True, action="reindex", message="queued"))
        except Exception as exc:
            results.append(BulkWorkflowActionResult(workflow_id=workflow_id, ok=False, action="reindex", message=str(exc)))

    return BulkWorkflowActionResponse(
        action="reindex",
        dry_run=request.dry_run,
        requested=len(request.workflow_ids),
        succeeded=sum(1 for result in results if result.ok),
        failed=sum(1 for result in results if not result.ok),
        results=results,
    )


@router.post("/documents/{workflow_id}/approve-ocr")
async def approve_ocr(workflow_id: str, user: RequireReview):
    """Approve OCR results and continue to chunking. Requires permission: review."""
    _require_document_for_user(workflow_id, user, Permission.REVIEW)
    handle = await _validate_approval_stage(workflow_id, "ocr_review")
    await handle.signal(DocumentPipelineWorkflow.approve_ocr)

    # Log approval
    _log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="ocr_approved",
        new_value=True,
        metadata={"stage": "ocr_review", "next_stage": "translation_processing"},
        user=user,
    )

    return {"approved": "ocr", "workflow_id": workflow_id}


@router.post("/documents/{workflow_id}/approve-chunks")
async def approve_chunks(workflow_id: str, user: RequireReview):
    """Approve chunks and continue to prepare for ingestion."""
    _require_document_for_user(workflow_id, user, Permission.REVIEW)
    handle = await _validate_approval_stage(workflow_id, "chunk_review")
    await handle.signal(DocumentPipelineWorkflow.approve_chunks)

    # Log approval
    _log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="chunks_approved",
        new_value=True,
        metadata={"stage": "chunk_review", "next_stage": "ready_for_ingestion"},
        user=user,
    )

    return {"approved": "chunks", "workflow_id": workflow_id}


@router.post("/documents/{workflow_id}/approve-translation")
async def approve_translation(workflow_id: str, user: RequireReview):
    """Approve translations and continue to chunking."""
    _require_document_for_user(workflow_id, user, Permission.REVIEW)
    handle = await _validate_approval_stage(workflow_id, "translation_review")
    await handle.signal(DocumentPipelineWorkflow.approve_translation)

    # Log approval
    _log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="translation_approved",
        new_value=True,
        metadata={"stage": "translation_review", "next_stage": "chunking"},
        user=user,
    )

    return {"approved": "translation", "workflow_id": workflow_id}


@router.post("/documents/{workflow_id}/approve-ingestion")
async def approve_ingestion(workflow_id: str, user: RequireApproveIngestion):
    """Approve ingestion and continue to vector-index ingestion.

    Requires APPROVE_INGESTION, not REVIEW: a ``state_contributor`` may approve
    the OCR / translation / chunking gates but must not publish to DEV.
    """
    _require_document_for_user(workflow_id, user, Permission.APPROVE_INGESTION)
    handle = await _validate_approval_stage(workflow_id, "ready_for_ingestion")
    await handle.signal(DocumentPipelineWorkflow.approve_ingestion)

    # Log approval
    _log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="ingestion_approved",
        new_value=True,
        metadata={"stage": "ready_for_ingestion", "next_stage": "ingesting"},
        user=user,
    )

    return {"approved": "ingestion", "workflow_id": workflow_id}


@router.post("/documents/{workflow_id}/request-prod-ready")
async def request_prod_ready(workflow_id: str, user: RequireReview):
    """
    state_admin: flag this document as ready for a super_admin to review for
    PROD promotion. Purely informational — does not itself fire approve_prod
    or promote anything; the super_admin still makes that call via
    POST /documents/{id}/approve-prod. Mirrors the "request ingest" pattern
    used elsewhere in this pipeline: a request is a separate, weaker signal
    from the actual privileged action.
    """
    doc = _require_document_for_user(workflow_id, user, Permission.REVIEW)
    stage = doc.get("stage")
    if stage != "approval_for_prod":
        raise HTTPException(
            400,
            f"Cannot request prod-ready: document is in '{stage}' stage "
            "(expected 'approval_for_prod').",
        )

    updated = db.mark_prod_ready_requested(
        workflow_id,
        user_id=getattr(user, "user_id", None),
        username=getattr(user, "username", None),
    )

    _log_audit(
        workflow_id=workflow_id,
        action_type="request_prod_ready",
        entity_type="document",
        field_name="prod_ready_requested_at",
        new_value=updated.get("prod_ready_requested_at") if updated else None,
        metadata={"stage": stage},
        user=user,
    )

    return {
        "requested": True,
        "workflow_id": workflow_id,
        "prod_ready_requested_at": updated.get("prod_ready_requested_at") if updated else None,
        "prod_ready_requested_by_username": updated.get("prod_ready_requested_by_username") if updated else None,
    }


@router.post("/documents/{workflow_id}/approve-prod")
async def approve_prod(
    workflow_id: str,
    user: RequireAdmin,
):
    """Superadmin-only: promote DEV-ingested vectors into PROD Qdrant."""
    if index_only_mode():
        raise HTTPException(409, "Promotion is disabled in index-only mode")
    doc = _require_document_for_user(workflow_id, user)
    if doc.get("uses_network_releases"):
        raise HTTPException(409, "This upload uses automatic release to its configured network")
    stage = doc.get("stage")
    # Promoting an already-completed document is a fresh PROD write, so it is
    # blocked outright when PROD is disabled. A document still parked at the
    # approval gate (started before the flag was set) is allowed through, or it
    # would be stranded there forever.
    if prod_stage_disabled() and stage != "approval_for_prod":
        raise HTTPException(
            400,
            "PROD promotion is disabled (DISABLE_PROD_SETTING=true). "
            "Documents finish at DEV ingest.",
        )
    if stage not in {"approval_for_prod", "completed"}:
        raise HTTPException(
            400,
            f"Cannot approve for prod: document is in '{stage}' stage "
            "(expected 'approval_for_prod' or 'completed').",
        )

    # Prefer signaling the running main workflow when it is waiting at approval_for_prod.
    signaled = False
    if stage == "approval_for_prod":
        try:
            handle = await _validate_approval_stage(workflow_id, "approval_for_prod")
            await handle.signal(DocumentPipelineWorkflow.approve_prod)
            signaled = True
        except HTTPException:
            signaled = False
        except Exception:
            signaled = False

    promote_workflow_id = None
    if not signaled:
        import time

        promote_workflow_id = f"{workflow_id}-promote-prod-{int(time.time())}"
        await _api.temporal_client.start_workflow(
            PromoteToProdWorkflow.run,
            args=[
                doc.get("document_id") or workflow_id,
                doc.get("filename") or workflow_id,
                workflow_id,
                int(doc.get("page_count") or 0),
                int(doc.get("chunk_count") or 0),
            ],
            id=promote_workflow_id,
            task_queue=_api.TASK_QUEUE,
        )
        db.create_document_job(
            workflow_id=workflow_id,
            job_type="promote_prod",
            temporal_workflow_id=promote_workflow_id,
            status="running",
            current_stage="ingesting_prod",
            actor=_actor_from_user(user),
        )
        db.update_document_stage(
            workflow_id=workflow_id,
            stage="ingesting_prod",
            page_count=doc.get("page_count"),
            chunk_count=doc.get("chunk_count"),
        )

    if doc.get("prod_ready_requested_at"):
        db.clear_prod_ready_requested(workflow_id)

    _log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="prod_approved",
        new_value=True,
        metadata={
            "stage": stage,
            "next_stage": "ingesting_prod",
            "signaled": signaled,
            "promote_workflow_id": promote_workflow_id,
        },
        user=user,
    )
    return {
        "approved": "prod",
        "workflow_id": workflow_id,
        "signaled": signaled,
        "promote_workflow_id": promote_workflow_id,
        "next_stage": "ingesting_prod",
    }
