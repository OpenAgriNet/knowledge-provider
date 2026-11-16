"""Document detail, processing artifacts, and runtime routes."""

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from temporalio.client import WorkflowFailureError

from pipeline import db
from pipeline.api import api as _api
from pipeline.api.api import _list_available_actions, _parse_roles_field, _require_document_for_user
from pipeline.api.routers.document_content import _inline_content_disposition
from pipeline.auth.deps import CurrentUser, RequireSearch
from pipeline.auth.tenancy import normalize_instance
from pipeline.documents.models import (
    PIPELINE_STAGES,
    PUBLISH_SKIPPED_UNMAPPED_KIND,
    DocumentDetail,
    DocumentGraph,
    DocumentStage,
    NetworkPublishOutcome,
)
from pipeline.network.network_constants import RESULT_REJECTED
from pipeline.object_storage import is_object_uri, parse_object_uri
from pipeline.intake.source_intake import public_source_job

router = APIRouter()


NETWORK_PUBLISH_ARTIFACT_TYPES = {"network_publish_payload", "network_publish_skipped"}


def _network_publish_outcome(artifacts: list[dict]) -> NetworkPublishOutcome | None:
    latest = next((a for a in artifacts if a.get("artifact_type") in NETWORK_PUBLISH_ARTIFACT_TYPES), None)
    if latest is None:
        return None
    try:
        exchange = json.loads(latest.get("metadata_json") or "{}")
    except ValueError:
        exchange = {}
    if latest["artifact_type"] == "network_publish_skipped":
        return NetworkPublishOutcome(
            status="skipped",
            reason_code=exchange.get("reason_code") or PUBLISH_SKIPPED_UNMAPPED_KIND,
            reason=exchange.get("reason") or "No network catalog is mapped to this document kind.",
            created_at=latest.get("created_at"),
        )
    if exchange.get("result_status") == RESULT_REJECTED or exchange.get("failure_reason"):
        return NetworkPublishOutcome(
            status="rejected",
            reason=exchange.get("failure_reason") or f"The network rejected the catalog: {exchange.get('errors') or []}",
            created_at=latest.get("created_at"),
        )
    return NetworkPublishOutcome(status="published", created_at=latest.get("created_at"))


def _build_document_detail(doc: dict) -> DocumentDetail:
    workflow_id = doc["workflow_id"]
    current_job = db.get_latest_document_job(workflow_id)
    artifacts = db.list_document_artifacts(workflow_id)
    return DocumentDetail(
        id=doc.get("id"),
        document_id=doc["document_id"],
        canonical_document_id=doc.get("canonical_document_id"),
        workflow_id=workflow_id,
        filename=doc["filename"],
        name=doc.get("name"),
        source_filename=doc.get("source_filename"),
        source_manifest_name=doc.get("source_manifest_name"),
        source_file_fingerprint=doc.get("source_file_fingerprint"),
        authoritative=bool(doc.get("source_manifest_name")),
        instance=normalize_instance(doc.get("instance")),
        filepath=doc["filepath"],
        stage=DocumentStage(doc["stage"]),
        page_count=doc.get("page_count", 0),
        chunk_count=doc.get("chunk_count", 0),
        error_message=doc.get("error_message"),
        reindex_required=bool(doc.get("reindex_required")),
        reindex_reason=doc.get("reindex_reason"),
        available_actions=_list_available_actions(doc, current_job),
        translated_count=db.count_translated_pages(workflow_id),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
        ocr_completed_at=doc.get("ocr_completed_at"),
        translation_completed_at=doc.get("translation_completed_at"),
        chunks_completed_at=doc.get("chunks_completed_at"),
        ingested_at=doc.get("ingested_at"),
        source_type=doc.get("source_type"),
        canonical_input_type=doc.get("canonical_input_type"),
        stop_after_ocr=bool(doc.get("stop_after_ocr")),
        original_artifact_id=doc.get("original_artifact_id"),
        normalized_artifact_id=doc.get("normalized_artifact_id"),
        latest_job_id=doc.get("latest_job_id"),
        current_job=public_source_job(current_job) if current_job else None,
        artifacts=artifacts,
        index_status=db.list_document_index_status(workflow_id),
        network_publish=_network_publish_outcome(artifacts),
        crop_subjects=doc.get("crop_subjects") or [],
        knowledge_metadata=doc.get("knowledge_metadata") or {},
        uploaded_by_user_id=doc.get("uploaded_by_user_id"),
        uploaded_by_username=doc.get("uploaded_by_username"),
        uploaded_by_email=doc.get("uploaded_by_email"),
        uploaded_by_roles=_parse_roles_field(doc.get("uploaded_by_roles")),
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

def _build_stage_io_payload(workflow_id: str, current_stage: Optional[str] = None) -> dict:
    artifacts = db.list_document_artifacts(workflow_id)
    grouped: dict[str, dict] = {}
    for stage_id, label, description in PIPELINE_STAGES:
        grouped[stage_id] = {
            "stage": stage_id,
            "label": label,
            "description": description,
            "input_artifacts": [],
            "output_artifacts": [],
        }

    input_types = {"original_upload", "normalized_pdf", "normalized_spreadsheet", "normalized_text"}
    for artifact in artifacts:
        stage = artifact.get("stage") or "registered"
        if stage not in grouped:
            grouped[stage] = {
                "stage": stage,
                "label": stage.replace("_", " ").title(),
                "description": "",
                "input_artifacts": [],
                "output_artifacts": [],
            }
        bucket = "input_artifacts" if artifact["artifact_type"] in input_types else "output_artifacts"
        grouped[stage][bucket].append(artifact)

    return {
        "workflow_id": workflow_id,
        "current_stage": current_stage,
        "stages": list(grouped.values()),
    }


async def _get_runtime_payload(workflow_id: str, doc: Optional[dict] = None) -> dict:
    doc = doc or db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Document not found: {workflow_id}")
    current_job = db.get_latest_document_job(workflow_id)
    runtime_workflow_id = (
        current_job.get("temporal_workflow_id")
        if current_job and current_job.get("status") == "running" and current_job.get("temporal_workflow_id")
        else workflow_id
    )

    chunking_progress = None
    if current_job and current_job.get("config_json"):
        try:
            parsed_config = json.loads(current_job["config_json"]) if isinstance(current_job["config_json"], str) else current_job["config_json"]
            if isinstance(parsed_config, dict):
                chunking_progress = parsed_config.get("chunking_progress")
        except Exception:
            chunking_progress = None

    runtime = {
        "workflow_id": workflow_id,
        "sqlite_stage": doc.get("stage"),
        "sqlite_error_message": doc.get("error_message"),
        "temporal_connected": _api.temporal_client is not None,
        "job": public_source_job(current_job) if current_job else None,
        "chunking_progress": chunking_progress,
        "temporal": None,
    }

    if _api.temporal_client is None:
        return runtime

    try:
        handle = _api.temporal_client.get_workflow_handle(runtime_workflow_id)
        description = await handle.describe()
        temporal_state = None
        query_error = None
        try:
            temporal_state = await handle.query("get_state")
        except Exception as exc:
            query_error = str(exc)

        runtime["temporal"] = {
            "workflow_id": runtime_workflow_id,
            "run_id": description.run_id,
            "status": description.status.name,
            "close_time": description.close_time.isoformat() if description.close_time else None,
            "execution_time": description.execution_time.isoformat() if description.execution_time else None,
            "state": temporal_state,
            "query_error": query_error,
        }
    except Exception as exc:
        runtime["temporal"] = {
            "workflow_id": workflow_id,
            "status": "UNAVAILABLE",
            "error": str(exc),
        }

    return runtime


@router.get("/documents/{workflow_id}", response_model=DocumentDetail)
async def get_document(workflow_id: str, user: CurrentUser):
    """Get document workflow state with artifacts and indexing metadata."""
    doc = _require_document_for_user(workflow_id, user)
    return _build_document_detail(doc)


@router.get("/documents/{workflow_id}/error-details")
async def get_workflow_error_details(workflow_id: str, user: RequireSearch):
    """
    Get detailed error information from Temporal for a failed workflow.

    Returns comprehensive error details including:
    - Error message
    - Stack trace (if available)
    - Failure type
    - Workflow execution status

    This endpoint queries Temporal directly to get the most detailed
    error information available, which may be more complete than what's
    stored in Postgres.
    """
    import traceback

    # Enforce tenant scope before touching Temporal (404 hides other tenants).
    _require_document_for_user(workflow_id, user)

    try:
        handle = _api.temporal_client.get_workflow_handle(workflow_id)
        description = await handle.describe()

        result = {
            "workflow_id": workflow_id,
            "run_id": description.run_id,
            "status": description.status.name,
            "error_message": None,
            "error_type": None,
            "stack_trace": None,
            "has_error": False
        }

        # If workflow is failed, try to get detailed error information
        if description.status.name == "FAILED":
            result["has_error"] = True

            # Try to get error from workflow result (this raises WorkflowFailureError for failed workflows)
            try:
                await handle.result()
            except WorkflowFailureError as wf_err:
                # Extract error details from the failure
                result["error_message"] = str(wf_err)
                result["error_type"] = type(wf_err).__name__

                # Try to get the underlying cause
                if hasattr(wf_err, 'cause') and wf_err.cause:
                    cause = wf_err.cause
                    result["error_message"] = str(cause)
                    result["error_type"] = type(cause).__name__

                    # Get stack trace if available
                    if hasattr(cause, '__traceback__') and cause.__traceback__:
                        result["stack_trace"] = ''.join(traceback.format_tb(cause.__traceback__))
                    elif hasattr(wf_err, '__traceback__') and wf_err.__traceback__:
                        result["stack_trace"] = ''.join(traceback.format_tb(wf_err.__traceback__))

                # Also try to get failure details from the exception itself
                if hasattr(wf_err, 'failure') and wf_err.failure:
                    failure = wf_err.failure
                    if hasattr(failure, 'message') and failure.message:
                        result["error_message"] = failure.message
                    if hasattr(failure, 'stack_trace') and failure.stack_trace:
                        result["stack_trace"] = failure.stack_trace
            except Exception as e:
                # If result() doesn't work, try other methods
                result["error_message"] = f"Could not retrieve error details: {str(e)}"

        # Also try to get error from workflow state query (fallback)
        if not result["error_message"]:
            try:
                state = await handle.query("get_state")
                if state and state.get("error_message"):
                    result["error_message"] = state.get("error_message")
                    result["has_error"] = True
            except Exception:
                pass  # Workflow might not support queries or be in wrong state

        return result

    except Exception as e:
        # If workflow doesn't exist or can't be accessed
        error_msg = str(e)
        if "not found" in error_msg.lower() or "workflow" in error_msg.lower():
            raise HTTPException(404, f"Workflow not found: {workflow_id}")
        raise HTTPException(500, f"Error fetching workflow details: {error_msg}")


@router.get("/documents/{workflow_id}/runtime")
async def get_document_runtime(workflow_id: str, user: RequireSearch):
    """Return live runtime status by combining Postgres state and Temporal workflow state."""
    doc = _require_document_for_user(workflow_id, user)
    return await _get_runtime_payload(workflow_id, doc=doc)


@router.get("/documents/{workflow_id}/artifacts")
async def list_document_artifacts(workflow_id: str, user: RequireSearch):
    _require_document_for_user(workflow_id, user)
    return db.list_document_artifacts(workflow_id)


@router.get("/documents/{workflow_id}/artifacts/{artifact_id}")
async def get_document_artifact(workflow_id: str, user: RequireSearch, artifact_id: str):
    _require_document_for_user(workflow_id, user)
    artifact = db.get_document_artifact(workflow_id, artifact_id)
    if not artifact:
        raise HTTPException(404, f"Artifact not found: {artifact_id}")
    return artifact


@router.get("/documents/{workflow_id}/artifacts/{artifact_id}/content")
async def get_document_artifact_content(workflow_id: str, user: RequireSearch, artifact_id: str):
    _require_document_for_user(workflow_id, user)
    artifact = db.get_document_artifact(workflow_id, artifact_id)
    if not artifact:
        raise HTTPException(404, f"Artifact not found: {artifact_id}")

    storage_uri = artifact["storage_uri"]
    filename = artifact.get("filename") or "artifact"
    media_type = artifact.get("mime_type") or "application/octet-stream"
    disposition = _inline_content_disposition(filename)
    if Path(filename).suffix.lower() in {".html", ".htm"} or media_type.split(";")[0].strip().lower() in {
        "text/html", "application/xhtml+xml",
    }:
        media_type = "application/octet-stream"
        disposition = disposition.replace("inline", "attachment", 1)
    headers = {"Content-Disposition": disposition, "X-Content-Type-Options": "nosniff"}
    if is_object_uri(storage_uri):
        response = _api.object_store.open(parse_object_uri(storage_uri))
        return StreamingResponse(response, media_type=media_type, headers=headers)

    file_path = Path(storage_uri)
    if not file_path.exists():
        raise HTTPException(404, "Artifact content not found")
    return StreamingResponse(open(file_path, "rb"), media_type=media_type, headers=headers)


@router.get("/documents/{workflow_id}/jobs")
async def list_document_jobs(workflow_id: str, user: RequireSearch, limit: int = Query(20, le=100)):
    _require_document_for_user(workflow_id, user)
    return [public_source_job(job) for job in db.list_document_jobs(workflow_id, limit=limit)]


@router.get("/documents/{workflow_id}/stage-io")
async def get_document_stage_io(workflow_id: str, user: RequireSearch):
    doc = _require_document_for_user(workflow_id, user)
    return _build_stage_io_payload(workflow_id, current_stage=doc.get("stage"))


@router.get("/documents/{workflow_id}/allowed-actions")
async def get_document_allowed_actions(workflow_id: str, user: RequireSearch):
    """Return the currently valid machine-facing actions for a document."""
    doc = _require_document_for_user(workflow_id, user)
    return {
        "workflow_id": workflow_id,
        "stage": doc.get("stage"),
        "reindex_required": bool(doc.get("reindex_required")),
        "available_actions": _list_available_actions(doc, db.get_latest_document_job(workflow_id)),
    }


@router.get("/documents/{workflow_id}/graph", response_model=DocumentGraph)
async def get_document_graph(workflow_id: str, user: RequireSearch):
    """Return a document-centric graph of state, jobs, artifacts, index status, and runtime."""
    doc = _require_document_for_user(workflow_id, user)
    detail = _build_document_detail(doc)
    return DocumentGraph(
        workflow_id=workflow_id,
        document=detail,
        jobs=[public_source_job(job) for job in db.list_document_jobs(workflow_id, limit=100)],
        artifacts=detail.artifacts,
        index_status=detail.index_status,
        stage_io=_build_stage_io_payload(workflow_id, current_stage=doc.get("stage")),
        runtime=await _get_runtime_payload(workflow_id, doc=doc),
    )
