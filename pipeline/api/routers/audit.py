"""Provider-scoped and document-scoped audit routes."""

from fastapi import APIRouter, Query

from pipeline import db
from pipeline.api.api import _instance_scope_for_user, _require_document_for_user
from pipeline.auth.deps import RequireSearch
from pipeline.documents.models import AuditLogResponse

router = APIRouter()


@router.get("/audit", response_model=AuditLogResponse)
async def get_all_audit_logs(
    user: RequireSearch,
    action_type: str = None,
    limit: int = Query(50, le=200),
    offset: int = 0
):
    """
    Get global audit trail across all documents.

    Returns a list of all changes including:
    - Stage transitions
    - Page edits
    - Chunk edits
    - Approvals
    - Resets

    Each entry includes the document filename for context.
    """
    scope = _instance_scope_for_user(user)
    logs = db.get_all_audit_logs(
        action_type=action_type,
        limit=limit,
        offset=offset,
        instances=scope,
    )
    total = db.get_all_audit_log_count(action_type, instances=scope)

    return AuditLogResponse(
        logs=logs,
        total=total,
        limit=limit,
        offset=offset
    )


@router.get("/documents/{workflow_id}/audit", response_model=AuditLogResponse)
async def get_document_audit_log(
    workflow_id: str,
    user: RequireSearch,
    action_type: str = None,
    limit: int = Query(50, le=200),
    offset: int = 0
):
    """
    Get audit trail for a document.

    Returns a list of all changes made to the document including:
    - Stage transitions
    - Page edits
    - Chunk edits
    - Approvals
    - Resets
    """
    _require_document_for_user(workflow_id, user)
    logs = db.get_audit_logs(
        workflow_id=workflow_id,
        action_type=action_type,
        limit=limit,
        offset=offset
    )
    total = db.get_audit_log_count(workflow_id, action_type)

    return AuditLogResponse(
        logs=logs,
        total=total,
        limit=limit,
        offset=offset
    )
