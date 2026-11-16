"""Operator queue, runs, and workflow stage routes."""

from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

from pipeline import db
from pipeline.api.api import (
    _instance_scope_for_user,
    _list_available_actions,
    _require_document_for_user,
)
from pipeline.auth.deps import RequireSearch
from pipeline.auth.tenancy import unrestricted
from pipeline.network.release_policy import prod_stage_disabled
from pipeline.documents.models import (
    PIPELINE_STAGES,
    PROD_ONLY_STAGES,
    OperationQueueEntry,
    OperationQueueResponse,
)
from pipeline.intake.source_intake import public_source_job

router = APIRouter()


@router.get("/operations/queue", response_model=OperationQueueResponse)
async def get_operations_queue(
    user: RequireSearch,
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled"),
):
    """Return documents that currently need operator or agent action.

    Results are limited to instances the caller can access (state users never
    see portal ``bv`` or other states' documents).
    """
    include_disabled = x_include_disabled and x_include_disabled.lower() == "true"
    rows, total = db.list_operations_queue(
        limit=limit,
        offset=offset,
        include_disabled=include_disabled,
        instances=_instance_scope_for_user(user),
    )
    items = [
        OperationQueueEntry(
            workflow_id=row["workflow_id"],
            filename=row["filename"],
            stage=row["stage"],
            job_id=row.get("job_id"),
            job_type=row.get("job_type"),
            job_status=row.get("job_status"),
            started_at=row.get("started_at"),
            error_message=row.get("error_message") or row.get("reindex_reason"),
            available_actions=_list_available_actions(row, row),
        )
        for row in rows
    ]
    return OperationQueueResponse(items=items, total=total)


@router.get("/runs")
async def list_runs(
    user: RequireSearch,
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    status: Optional[str] = None,
):
    """List recent document jobs visible to the caller (tenant/role scoped).

    Restricted users only see jobs whose document ``instance`` is in their
    allowed states. Super-admin / bypass users see all tenants.
    """
    return [public_source_job(job) for job in db.list_runs(
        limit=limit,
        offset=offset,
        status=status,
        instances=_instance_scope_for_user(user),
    )]



@router.get("/runs/{job_id}")
async def get_run(job_id: str, user: RequireSearch):
    """Get a specific document job/run (404 if missing or outside tenant scope)."""
    run = db.get_document_job(job_id)
    if not run:
        raise HTTPException(404, f"Run not found: {job_id}")
    # Enforce same instance scope as list: hide other tenants' runs as 404.
    workflow_id = run.get("workflow_id")
    if workflow_id:
        _require_document_for_user(str(workflow_id), user)
    elif not unrestricted(user):
        raise HTTPException(404, f"Run not found: {job_id}")
    return public_source_job(run)

@router.get("/pipeline/stages")
async def get_pipeline_stages(user: RequireSearch):
    """Get the pipeline stages for UI stepper display.

    The PROD stages are omitted when DISABLE_PROD_SETTING is on, so the stepper
    shows the pipeline the documents will actually follow.
    """
    prod_disabled = prod_stage_disabled()
    return [
        {"id": stage[0], "label": stage[1], "description": stage[2]}
        for stage in PIPELINE_STAGES
        if not (prod_disabled and stage[0] in PROD_ONLY_STAGES)
    ]
