from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile

from .. import db, document_validity
from .. import display_name as display_name_rules
from ..auth.deps import RequireUpload
from ..auth.tenancy import assert_document_group_access, normalize_group
from ..group_names import prod_stage_disabled
from ..models import DocumentStage, DocumentSummary
from ..workflows import DocumentPipelineWorkflow
from . import api as _api

router = APIRouter()


@router.post("/upload", response_model=DocumentSummary)
@_api.limiter.limit(_api.RATE_LIMIT_UPLOAD)
async def upload_and_process(
    request: Request,  # Required for rate limiting
    user: RequireUpload,
    file: UploadFile = File(...),
    auto_approve: bool = False,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
    index_name: str = "documents-index",
    stop_after_ocr: bool = False,
    instance: str = "",
    kind: str = Query(..., description="scheme or advisory"),
    code: str = "",
    display_name: Optional[str] = Form(None),
):
    """
    Upload a supported file and start processing workflow.

    The file is stored in MinIO and then processed through the pipeline.
    Validates both file extension and PDF magic bytes for security.
    Rate limited to 10 requests/minute per IP.
    Requires permission: upload (no-op while AUTH_DISABLED=true).
    """
    create_group = _api._resolve_upload_instance(user, instance)
    doc_kind, doc_code, replace_doc = _api._resolve_document_identity(
        user, create_group, kind, code, file.filename or ""
    )
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _api.ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {suffix}")
    try:
        display_name = display_name_rules.normalize(display_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # Read file content
    content = await file.read()
    file_size = len(content)

    # Validate PDF magic bytes (%PDF-) only for PDF uploads
    if suffix == ".pdf":
        pdf_magic = b"%PDF-"
        if len(content) < 5 or content[:5] != pdf_magic:
            raise HTTPException(400, "Invalid PDF file: file does not have valid PDF header")

    # Generate unique object name
    file_hash = hashlib.md5(content).hexdigest()
    object_name = f"{file_hash}/{file.filename}"

    # Upload to MinIO
    content_type = "application/pdf" if suffix == ".pdf" else "application/octet-stream"
    _api.minio_client.put_object(
        _api.MINIO_BUCKET,
        object_name,
        BytesIO(content),
        length=file_size,
        content_type=content_type
    )

    # Use minio:// URI as filepath
    minio_path = f"minio://{_api.MINIO_BUCKET}/{object_name}"

    workflow_id = _api.get_workflow_id(minio_path)
    document_id = file_hash
    canonical_document_id = file_hash

    # Reuse only when Postgres still tracks this workflow.
    # If Postgres was purged, avoid returning stale Temporal state and create a fresh run ID.
    existing_doc = db.get_document(workflow_id)
    if existing_doc:
        existing_doc = assert_document_group_access(user, existing_doc)
        try:
            handle = _api.temporal_client.get_workflow_handle(workflow_id)
            state = await handle.query("get_state")
            if state:
                return DocumentSummary(
                    document_id=document_id,
                    canonical_document_id=canonical_document_id,
                    workflow_id=workflow_id,
                    filename=file.filename,
                    source_filename=file.filename,
                    source_file_fingerprint=file_hash,
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
            pass
    else:
        workflow_id = _api._rerun_workflow_id(workflow_id)

    # Start new workflow
    handle = await _api.temporal_client.start_workflow(
        DocumentPipelineWorkflow.run,
        args=[
            document_id,
            file.filename,
            minio_path,
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
    # See the manifest upload path above: the period starts at the upload.
    validity = document_validity.default_period()
    if replace_doc:
        document_uuid = replace_doc["id"]
        db.reset_document_for_reupload(
            document_uuid,
            filename=file.filename,
            filepath=minio_path,
            fingerprint=file_hash,
        )
    else:
        try:
            document_uuid = db.upsert_document(
                workflow_id=workflow_id,
                document_id=document_id,
                canonical_document_id=canonical_document_id,
                filename=file.filename,
                source_filename=file.filename,
                source_file_fingerprint=file_hash,
                filepath=minio_path,
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
                # v2: the contributor's Display Name is documents.name.
                name=display_name or _api._document_title(file.filename or ""),
            )
        except db.DuplicateDocumentCode as exc:
            raise HTTPException(409, str(exc)) from exc
    job_id = db.create_document_job(
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
    db.add_document_artifact(
        workflow_id=workflow_id,
        job_id=job_id,
        artifact_type="original_upload",
        stage="registered",
        storage_uri=minio_path,
        mime_type=content_type,
        filename=file.filename,
        size_bytes=file_size,
        metadata={"uploaded_via": "upload_endpoint", "uploaded_by": actor},
    )
    _api._log_audit(
        workflow_id=workflow_id,
        action_type="document_upload",
        entity_type="document",
        field_name="registered",
        new_value=file.filename,
        metadata={
            "source": "upload_endpoint",
            "instance": create_group,
            "kind": doc_kind,
            "code": doc_code,
            "replaced_existing": bool(replace_doc),
            "size_bytes": file_size,
            "auto_approve": auto_approve,
        },
        user=user,
    )
    source_type = "spreadsheet" if suffix in {".csv", ".xlsx"} else "document"
    canonical_input_type = "spreadsheet" if suffix in {".csv", ".xlsx"} else "pdf"
    db.update_document_fields(
        workflow_id,
        source_type=source_type,
        canonical_input_type=canonical_input_type,
        stop_after_ocr=1 if stop_after_ocr else 0,
    )

    return DocumentSummary(
        document_id=document_id,
        canonical_document_id=canonical_document_id,
        workflow_id=workflow_id,
        filename=file.filename,
        source_filename=file.filename,
        source_file_fingerprint=file_hash,
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
