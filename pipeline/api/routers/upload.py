from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from pipeline import db
from pipeline.documents import document_validity
from pipeline.documents import display_name as display_name_rules
from pipeline.auth.deps import RequireUpload
from pipeline.auth.models import AuthUser
from pipeline.auth.permissions import Permission
from pipeline.auth.tenancy import (
    assert_document_instance_access,
    configured_portal_instance,
    normalize_instance,
    portal_instance,
)
from pipeline.metadata.crop_metadata import parse_crop_subjects
from pipeline.documents.document_dispatch import start_registered_document
from pipeline.documents.document_formats import TEXT_UPLOAD_TYPES, allowed_file_extensions
from pipeline.documents.document_kinds import configured_document_kinds
from pipeline.intake.source_fetch import MAX_HTML_BYTES, extract_text_source
from pipeline.network.release_policy import (
    index_only_mode,
    intake_index_options,
    prod_stage_disabled,
    upload_release_target,
)
from pipeline.metadata.knowledge_metadata import KnowledgeMetadata, parse_knowledge_metadata
from pipeline.documents.models import DocumentStage, DocumentSummary
from pipeline.object_storage import ObjectReference
from pipeline.ocr.service import allowed_ocr_providers, default_ocr_profile
from pipeline.metadata.schema_packs import validate_schema_selection
from pipeline.intake.source_intake import attach_source_response
from pipeline.intake.webhook_mapping import parse_canonical_document
from pipeline.temporal.workflows import DocumentPipelineWorkflow
from pipeline.api import api as _api

router = APIRouter()
MAX_BATCH_FILES = 10
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
PENDING_START = "The file was saved, but processing could not be confirmed. Retry starting the document."


class BatchUploadItem(BaseModel):
    filename: str
    status: int
    document: DocumentSummary | None = None
    detail: str | None = None


class BatchUploadResult(BaseModel):
    items: list[BatchUploadItem]


@router.get("/upload/options")
def upload_options(user: RequireUpload):
    try:
        return {
            "parser_providers": allowed_ocr_providers(),
            "default_parser": default_ocr_profile(),
            "document_kinds": configured_document_kinds(),
            "file_extensions": sorted(allowed_file_extensions()),
        }
    except ValueError as exc:
        _api.logger.error("Invalid document processing configuration: %s", exc)
        raise HTTPException(503, "Parser selection is misconfigured") from exc


@router.get("/upload/providers")
def upload_providers(user: RequireUpload):
    """List the provider choices available to file and URL intake."""
    providers = {
        normalize_instance(code)
        for code in user.instances
        if code.strip() and user.has_permission_for_instance(Permission.UPLOAD, code)
    }
    preferred = None
    if user.is_superadmin or user.token_disabled_mode:
        providers.update(row["provider"] for row in db.list_provider_instances(None))
        preferred = portal_instance() if user.token_disabled_mode else configured_portal_instance()
        if preferred:
            providers.add(preferred)
    return {"providers": sorted(providers), "default_provider": preferred}


@router.get("/upload/scheme-matches")
@_api.limiter.limit("30/minute")
def find_similar_schemes(
    request: Request, user: RequireUpload, instance: str, name: str = Query(min_length=2, max_length=300),
):
    """Find candidates without changing or merging a document."""
    from difflib import SequenceMatcher
    from unicodedata import normalize

    owner = _api._resolve_create_instance(user, instance)
    if not user.has_permission_for_instance(Permission.UPLOAD, owner):
        raise HTTPException(403, "Upload permission is required for this provider")

    def key(value: str) -> str:
        return " ".join("".join(c if c.isalnum() else " " for c in normalize("NFKC", value).casefold()).split())

    needle = key(name)
    if not needle:
        return {"matches": []}
    with db.get_connection() as conn:
        candidates = conn.execute(
            "SELECT d.id::text, d.name, d.code, d.aliases FROM documents d "
            "JOIN instances i ON d.provider_id = i.id "
            "WHERE lower(i.name) = %s AND d.kind = 'scheme' AND NOT d.is_disabled "
            "ORDER BY (lower(d.code) = lower(%s) OR EXISTS ("
            "SELECT 1 FROM unnest(d.aliases) AS alias WHERE lower(alias) = lower(%s))) DESC, "
            "similarity(lower(d.name), lower(%s)) DESC, d.code LIMIT 200",
            (owner, name, name, name),
        ).fetchall()
    matches = []
    for candidate in candidates:
        labels = (candidate["name"], candidate["code"], *(candidate["aliases"] or []))
        score = max(SequenceMatcher(None, needle, key(label)).ratio() for label in labels if label)
        if score >= 0.68:
            matches.append({"id": candidate["id"], "name": candidate["name"], "code": candidate["code"], "score": score})
    matches = sorted(matches, key=lambda row: (-row["score"], row["code"]))[:8]
    if len(matches) > 1:
        from math import sqrt

        from pipeline.vector_store.embeddings import embed_passages, embed_query

        def cosine(left, right):
            length = sqrt(sum(x * x for x in left) * sum(x * x for x in right))
            return sum(a * b for a, b in zip(left, right)) / length if length else 0.0

        try:
            query = embed_query(name)
            passages = embed_passages([f'{row["name"]} ({row["code"]})' for row in matches])
            for row, vector in zip(matches, passages):
                row["semantic_score"] = cosine(query, vector)
            matches.sort(key=lambda row: (row["score"] < 1, -row.get("semantic_score", 0), -row["score"]))
        except Exception:
            _api.logger.warning("Scheme similarity model unavailable; using name and code matches")
    return {"matches": matches}


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
    kind: str = Query(..., description="A configured document kind"),
    code: str = "",
    display_name: Optional[str] = Form(None),
    crop_subjects: Optional[str] = Form(None, description="JSON array of document-wide crop subject references"),
    knowledge_metadata: Optional[str] = Form(None, description="JSON object with agriculture metadata"),
    valid_from: Optional[str] = Form(None),
    valid_to: Optional[str] = Form(None),
    parser_provider: Optional[str] = Form(None),
):
    """
    Upload a supported file and start processing workflow.

    The pipeline stores the original file in object storage and queues processing.
    Validates file extensions and checks the header of PDF uploads.
    Rate limited to 10 requests/minute per IP.
    Requires permission: upload (no-op while AUTH_DISABLED=true).
    """
    return await _process_upload(
        user, file, instance, kind, code, display_name, crop_subjects,
        auto_approve=auto_approve, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
        min_tokens=min_tokens, index_name=index_name, stop_after_ocr=stop_after_ocr,
        knowledge_metadata=knowledge_metadata, valid_from=valid_from, valid_to=valid_to,
        parser_provider=parser_provider,
    )


@router.post("/upload/batch", response_model=BatchUploadResult, status_code=207)
@_api.limiter.limit(_api.RATE_LIMIT_UPLOAD)
async def upload_batch(
    request: Request,
    user: RequireUpload,
    files: list[UploadFile] = File(...),
    instance: str = "",
    kind: str = Query(..., description="Knowledge kind shared by this batch"),
    parser_provider: Optional[str] = Form(None),
    crop_subjects: Optional[str] = Form(None),
    knowledge_metadata: Optional[str] = Form(None),
    valid_from: Optional[str] = Form(None),
    valid_to: Optional[str] = Form(None),
):
    """Submit independent supported files in one request. Return the outcome for each file."""
    if not 1 <= len(files) <= MAX_BATCH_FILES:
        raise HTTPException(413, f"Send between 1 and {MAX_BATCH_FILES} files per batch")
    if any(file.size is None for file in files):
        raise HTTPException(400, "Could not determine the size of every file")
    if sum(file.size for file in files) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Batch files must total at most 100 MiB")
    instance = _api._resolve_create_instance(user, instance)
    if not user.has_permission_for_instance(Permission.UPLOAD, instance):
        raise HTTPException(403, f"No upload permission in instance: {instance}")
    items = []
    for file in files:
        filename = file.filename or ""
        try:
            document = await _process_upload(
                user, file, instance, kind, "", None, crop_subjects,
                parser_provider=parser_provider, knowledge_metadata=knowledge_metadata,
                valid_from=valid_from, valid_to=valid_to,
            )
        except HTTPException as exc:
            items.append(BatchUploadItem(filename=filename, status=exc.status_code, detail=str(exc.detail)))
        except Exception:
            _api.logger.exception("Batch file intake failed")
            items.append(BatchUploadItem(filename=filename, status=500, detail="Could not start this document"))
        else:
            items.append(BatchUploadItem(
                filename=filename, status=503 if "retry_upload_start" in document.available_actions else 202,
                document=document, detail=document.error_message,
            ))
    return BatchUploadResult(items=items)


async def _start_registered_upload(job: dict) -> None:
    await start_registered_document(_api.temporal_client, _api.TASK_QUEUE, job)


@router.post("/upload/{workflow_id}/start", status_code=202)
@_api.limiter.limit(_api.RATE_LIMIT_UPLOAD)
async def retry_registered_upload(request: Request, workflow_id: str, user: RequireUpload):
    doc = _api._require_document_for_user(workflow_id, user)
    if doc.get("is_disabled"):
        raise HTTPException(409, "Restore the document before retrying its upload")
    if not user.has_permission_for_instance(Permission.UPLOAD, doc["instance"]):
        raise HTTPException(403, "Upload permission is required for this provider")
    job = db.get_latest_document_job(workflow_id)
    if not job or job["temporal_workflow_id"] != workflow_id or not job["config"].get("upload_start_args"):
        raise HTTPException(404, "Registered upload not found")
    if job["status"] == "start_pending":
        try:
            await _start_registered_upload(job)
        except Exception as exc:
            _api.logger.exception("Could not confirm registered upload start workflow_id=%s", workflow_id)
            raise HTTPException(503, PENDING_START) from exc
    return {"workflow_id": workflow_id, "status": "started"}


async def _process_upload(
    user: AuthUser,
    file: UploadFile,
    instance: str,
    kind: str,
    code: str,
    display_name: str | None,
    crop_subjects: str | None,
    *,
    auto_approve: bool = False,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
    index_name: str = "documents-index",
    stop_after_ocr: bool = False,
    knowledge_metadata: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    parser_provider: str | None = None,
    source_url: str | None = None,
    source_capture: dict | None = None,
) -> DocumentSummary:
    if parser_provider is not None:
        parser_provider = parser_provider.strip().lower()
        try:
            allowed_parsers = allowed_ocr_providers()
        except ValueError as exc:
            _api.logger.error("Invalid OCR provider configuration: %s", exc)
            raise HTTPException(503, "Parser selection is misconfigured") from exc
        if parser_provider not in allowed_parsers:
            raise HTTPException(422, "Parser provider is not enabled for this deployment")
    try:
        crops = parse_crop_subjects(crop_subjects)
    except ValueError as exc:
        raise HTTPException(422, "Invalid crop_subjects: provide up to 32 unique subject references with a URI or a code/name descriptor") from exc
    try:
        metadata = parse_knowledge_metadata(knowledge_metadata)
        if metadata and "schema_pack" in metadata:
            with db.get_connection() as conn:
                validate_schema_selection(conn, metadata)
    except ValueError as exc:
        raise HTTPException(422, "Invalid knowledge metadata or schema attributes") from exc
    create_instance = _api._resolve_create_instance(user, instance)
    doc_kind, doc_code, replace_doc = _api._resolve_document_identity(
        user, create_instance, kind, code, file.filename or ""
    )
    index_name, auto_approve = intake_index_options(index_name, auto_approve)
    try:
        release_target = upload_release_target(doc_kind, provider=create_instance) if not stop_after_ocr else None
    except ValueError as exc:
        _api.logger.error("Invalid automatic upload release configuration: %s", exc)
        raise HTTPException(503, "Automatic upload release is misconfigured") from exc
    if release_target:
        release_network_id, index_name = release_target
        auto_approve = True
    else:
        release_network_id = None
    filename = file.filename or ""
    if (
        not filename or len(filename) > 255 or "/" in filename or chr(92) in filename
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise HTTPException(400, "Invalid filename")
    suffix = Path(filename).suffix.lower()
    if suffix not in allowed_file_extensions():
        raise HTTPException(400, f"Unsupported file type: {suffix}")
    try:
        display_name = display_name_rules.normalize(display_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # Read file content
    limit = MAX_HTML_BYTES if suffix in TEXT_UPLOAD_TYPES else MAX_UPLOAD_BYTES
    content = await file.read(limit + 1)
    file_size = len(content)
    if file_size > limit:
        raise HTTPException(413, f"Files of this type must be at most {limit // (1024 * 1024)} MiB")
    attribution_url = source_url
    if suffix in TEXT_UPLOAD_TYPES:
        try:
            if suffix == ".json":
                submission = parse_canonical_document(content)
                if "kind" in submission.model_fields_set and submission.kind != doc_kind:
                    raise ValueError("The JSON document kind must match the upload kind")
                if display_name is None:
                    display_name = display_name_rules.normalize(submission.title)
                if crops is None and submission.crop_subjects:
                    crops = [crop.model_dump(exclude_none=True) for crop in submission.crop_subjects]
                submitted_metadata = (
                    submission.knowledge_metadata if metadata is None
                    else submission.metadata_with_translation_labels(KnowledgeMetadata.model_validate(metadata))
                )
                if submitted_metadata is not None:
                    metadata = submitted_metadata.storage_value()
                    with db.get_connection() as conn:
                        validate_schema_selection(conn, metadata)
                valid_from = valid_from if valid_from is not None else submission.valid_from
                valid_to = valid_to if valid_to is not None else submission.valid_to
                attribution_url = submission.source_url
            else:
                extract_text_source(content, TEXT_UPLOAD_TYPES[suffix], filename)
        except (ValueError, RecursionError) as exc:
            raise HTTPException(422, "Invalid text file or canonical JSON document") from exc

    try:
        if replace_doc:
            start = valid_from if valid_from is not None else replace_doc.get("valid_from")
            end = valid_to if valid_to is not None else replace_doc.get("valid_to")
        else:
            start, end = valid_from, valid_to
        validity = document_validity.parse_period(start, end)
    except document_validity.DocumentValidityError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Validate PDF magic bytes (%PDF-) only for PDF uploads
    if suffix == ".pdf":
        pdf_magic = b"%PDF-"
        if len(content) < 5 or content[:5] != pdf_magic:
            raise HTTPException(400, "Invalid PDF file: file does not have valid PDF header")

    file_hash = hashlib.md5(content).hexdigest()
    document_id = file_hash
    canonical_document_id = file_hash
    legacy_path = f"minio://{_api.OBJECT_STORE_BUCKET}/{file_hash}/{filename}"
    workflow_id = _api.get_workflow_id(f"{legacy_path}:{doc_code}" if source_url else legacy_path)

    # Reuse only when Postgres still tracks this workflow.
    # If Postgres was purged, avoid returning stale Temporal state and create a fresh run ID.
    existing_doc = db.get_document(workflow_id)
    if existing_doc and normalize_instance(existing_doc.get("instance")) == create_instance:
        existing_doc = assert_document_instance_access(user, existing_doc)
        if crops is not None and crops != (existing_doc.get("crop_subjects") or []):
            raise HTTPException(409, "This upload already exists with different crop metadata; use a new source file to reupload")
        if metadata is not None and metadata != (existing_doc.get("knowledge_metadata") or {}):
            raise HTTPException(409, "This upload already exists with different agriculture metadata")
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
                    instance=normalize_instance(existing_doc.get("instance")),
                    stage=DocumentStage(state.get("stage", "registered")),
                    page_count=state.get("page_count", 0),
                    chunk_count=state.get("chunk_count", 0),
                    error_message=state.get("error_message"),
                    crop_subjects=existing_doc.get("crop_subjects") or [],
                    knowledge_metadata=existing_doc.get("knowledge_metadata") or {},
                )
        except HTTPException:
            raise
        except Exception:
            pass
    upload_id = uuid4().hex
    workflow_id = f"{workflow_id}-rerun-{upload_id}"
    object_name = f"uploads/{upload_id}/{filename}"
    ref = ObjectReference(_api.OBJECT_STORE_BUCKET, object_name)
    storage_uri = ref.to_uri()
    content_type = "application/pdf" if suffix == ".pdf" else "application/octet-stream"
    _api.object_store.put(ref, BytesIO(content), file_size, content_type)

    start_args = [
        document_id, filename, storage_uri, chunk_size, chunk_overlap,
        min_tokens, index_name, auto_approve, stop_after_ocr, prod_stage_disabled(),
    ]
    if release_network_id or parser_provider or index_only_mode():
        start_args.append(release_network_id)
    if parser_provider or index_only_mode():
        start_args.append(parser_provider)
    if index_only_mode():
        start_args.append(True)
    registered_start = index_only_mode() or not replace_doc or bool(release_network_id)
    if not registered_start:
        # The legacy replacement mutates existing content. Keep its start-before-reset order.
        await _api.temporal_client.start_workflow(
            DocumentPipelineWorkflow.run, args=start_args, id=workflow_id, task_queue=_api.TASK_QUEUE,
        )

    # Save to Postgres for visibility during processing
    actor = _api._actor_from_user(user)
    with db.get_connection() as conn:
        if replace_doc:
            document_uuid = replace_doc["id"]
            try:
                db.reset_document_for_reupload(
                    document_uuid,
                    filename=file.filename,
                    filepath=storage_uri,
                    fingerprint=file_hash,
                    crop_subjects=crops,
                    knowledge_metadata=metadata,
                    expected_job_id=replace_doc.get("latest_job_id") if release_network_id else None,
                    _conn=conn,
                )
            except db.DocumentReplacementConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            if valid_from is not None or valid_to is not None:
                db.update_document_fields(
                    document_uuid, valid_from=validity.start_date, valid_to=validity.end_date, _conn=conn,
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
                    filepath=storage_uri,
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
                    # v2: the contributor's Display Name is documents.name.
                    name=display_name or _api._document_title(file.filename or ""),
                    crop_subjects=crops,
                    knowledge_metadata=metadata,
                    _conn=conn,
                )
            except db.DuplicateDocumentCode as exc:
                raise HTTPException(409, str(exc)) from exc
        job_id = db.create_document_job(
            workflow_id=document_uuid,
            job_type="ocr_only" if stop_after_ocr else "pipeline",
            temporal_workflow_id=workflow_id,
            status="start_pending" if registered_start else "running",
            current_stage="registered",
            config={
                **({"upload_start_args": start_args} if registered_start else {}),
                "auto_approve": auto_approve,
                "release_network_id": release_network_id,
                **({"source_url": attribution_url} if attribution_url else {}),
                **({"parser_provider": parser_provider} if parser_provider else {}),
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
                "min_tokens": min_tokens,
                "index_name": index_name,
                "stop_after_ocr": stop_after_ocr,
                "uploaded_by": actor,
            },
            actor=actor,
            _conn=conn,
        )
        if source_capture is not None:
            attach_source_response(conn, job_id, source_capture)
        db.add_document_artifact(
            workflow_id=workflow_id,
            job_id=job_id,
            artifact_type="original_upload",
            stage="registered",
            storage_uri=storage_uri,
            mime_type=content_type,
            filename=file.filename,
            size_bytes=file_size,
            metadata={"uploaded_via": "url_intake" if source_url else "upload_endpoint", "uploaded_by": actor},
            _conn=conn,
        )
        if suffix in TEXT_UPLOAD_TYPES:
            source_type, canonical_input_type = "document", "structured_text"
        elif suffix in {".csv", ".xlsx"}:
            source_type = canonical_input_type = "spreadsheet"
        else:
            source_type, canonical_input_type = "document", "pdf"
        db.update_document_fields(
            workflow_id,
            source_type=source_type,
            canonical_input_type=canonical_input_type,
            stop_after_ocr=1 if stop_after_ocr else 0,
            _conn=conn,
        )

        _api._log_audit(
            workflow_id=workflow_id,
            action_type="document_upload",
            entity_type="document",
            field_name="registered",
            new_value=file.filename,
            metadata={
                "source": "url_intake" if source_url else "upload_endpoint",
                "instance": create_instance,
                "kind": doc_kind,
                "code": doc_code,
                "replaced_existing": bool(replace_doc),
                "size_bytes": file_size,
                "auto_approve": auto_approve,
                "release_network_id": release_network_id,
            },
            user=user,
            _conn=conn,
        )

    start_error = None
    if registered_start:
        try:
            await _start_registered_upload({
                "id": job_id,
                "config": {"upload_start_args": start_args},
                "temporal_workflow_id": workflow_id,
            })
        except Exception:
            _api.logger.exception("Could not confirm registered upload start workflow_id=%s", workflow_id)
            start_error = PENDING_START

    return DocumentSummary(
        id=document_uuid,
        document_id=document_id,
        canonical_document_id=canonical_document_id,
        workflow_id=workflow_id,
        filename=file.filename,
        source_filename=file.filename,
        source_file_fingerprint=file_hash,
        authoritative=False,
        instance=create_instance,
        stage=DocumentStage.REGISTERED,
        page_count=0,
        chunk_count=0,
        uploaded_by_user_id=actor.get("user_id") or None,
        uploaded_by_username=actor.get("username") or None,
        uploaded_by_email=actor.get("email") or None,
        uploaded_by_roles=actor.get("roles") or [],
        error_message=start_error,
        available_actions=["retry_upload_start"] if start_error else [],
        crop_subjects=crops if crops is not None else (replace_doc or {}).get("crop_subjects") or [],
        knowledge_metadata=metadata if metadata is not None else (replace_doc or {}).get("knowledge_metadata") or {},
    )
