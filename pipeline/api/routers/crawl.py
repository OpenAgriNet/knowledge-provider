from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from io import BytesIO

from fastapi import APIRouter, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pipeline import db
from pipeline.api import api as _api
from pipeline.api.routers.upload import _process_upload
from pipeline.auth.deps import RequireUpload
from pipeline.auth.permissions import Permission
from pipeline.metadata.crop_metadata import CropSubjects
from pipeline.intake.source_fetch import SourceFile, SourcePageError, fetch_source
from pipeline.network.release_policy import upload_release_target
from pipeline.metadata.knowledge_metadata import KnowledgeMetadata
from pipeline.intake.provider_sources import CrawlProfile
from pipeline.intake.source_intake import (
    IntakeConflict,
    NormalizedSourceDocument,
    attach_source_response,
    canonical_json_sha256,
    crawl_record_key,
    register_source_document,
    start_source_document,
    store_source_response,
)
from pipeline.intake.webhook_mapping import (
    CanonicalMapping,
    decode_source_json,
    load_intake_template,
    map_source_submission,
)

router = APIRouter()


class CrawlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(min_length=1, max_length=4096)
    kind: str = "advisory"
    external_id: str | None = Field(default=None, max_length=512)
    crop_subjects: CropSubjects = Field(default_factory=list)
    knowledge_metadata: KnowledgeMetadata | None = None
    valid_from: str | None = None
    valid_to: str | None = None



def _crawl_provider(user, provider: str) -> str:
    instance = _api._resolve_create_instance(user, provider)
    if not user.has_permission_for_instance(Permission.UPLOAD, instance):
        raise HTTPException(403, "Upload permission is required for this provider")
    return instance


def _crawl_profiles(instance: str) -> dict[str, CrawlProfile]:
    from pipeline.intake.provider_sources import stored_settings

    try:
        stored = stored_settings(instance)
        if stored:
            return {key: CrawlProfile.model_validate(profile) for key, profile in stored["settings"]["crawl"].items()}
        providers = json.loads(os.environ.get("CRAWL_SOURCE_PROFILES") or "{}")
        return {
            key: CrawlProfile.model_validate(profile)
            for key, profile in providers.get(instance, {}).items()
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", key)
        }
    except (AttributeError, ValueError) as exc:
        raise HTTPException(503, "The crawl source configuration is invalid") from exc


@router.get("/providers/{provider}/crawl")
def crawl_sources(provider: str, user: RequireUpload):
    profiles = _crawl_profiles(_crawl_provider(user, provider))
    return {"sources": [{"id": key, "host": profile.host} for key, profile in sorted(profiles.items())]}


@router.post("/providers/{provider}/crawl", status_code=202)
@_api.limiter.limit(_api.RATE_LIMIT_UPLOAD)
async def crawl_public_source(request: Request, provider: str, data: CrawlRequest, user: RequireUpload):
    instance = _crawl_provider(user, provider)
    # No saved profile means no outbound credentials. A URL-specific identity
    # keeps unrelated public feeds from sharing external IDs.
    source_id = "public-" + canonical_json_sha256(data.url)[:32]
    return await _ingest_url(instance, source_id, data, user)


@router.post("/providers/{provider}/crawl/{source_id}", status_code=202)
@_api.limiter.limit(_api.RATE_LIMIT_UPLOAD)
async def crawl_source(request: Request, provider: str, source_id: str, data: CrawlRequest, user: RequireUpload):
    instance = _crawl_provider(user, provider)
    profile = _crawl_profiles(instance).get(source_id)
    if profile is None:
        raise HTTPException(404, "Crawl source not found")
    return await _ingest_url(instance, source_id, data, user, profile)


async def _ingest_url(instance: str, source_id: str, data: CrawlRequest, user, profile: CrawlProfile | None = None):
    authorization = os.environ.get(profile.authorization_env) if profile and profile.authorization_env else None
    if profile and profile.authorization_env and not authorization:
        raise HTTPException(503, "The crawl source credential is unavailable")
    try:
        source = await fetch_source(data.url, {profile.host.lower()} if profile else None, authorization)
    except SourcePageError as exc:
        raise HTTPException(422, str(exc)) from exc
    capture = await asyncio.to_thread(
        store_source_response, _api.object_store, _api.OBJECT_STORE_BUCKET, instance, source,
    )
    if isinstance(source, SourceFile) and source.media_type != "application/json":
        source_key = canonical_json_sha256([source_id, data.external_id or data.url])[:32]
        code = f"crawl-{source_key}"
        existing = db.find_document_by_code(instance, data.kind.strip().lower(), code)
        crops = [crop.model_dump(exclude_none=True) for crop in data.crop_subjects]
        metadata = data.knowledge_metadata.storage_value() if data.knowledge_metadata else None
        if (
            existing and not existing["is_disabled"]
            and existing.get("source_file_fingerprint") == hashlib.md5(source.content).hexdigest()
            and ("crop_subjects" not in data.model_fields_set or crops == (existing.get("crop_subjects") or []))
            and (metadata is None or metadata == (existing.get("knowledge_metadata") or {}))
            and (data.valid_from is None or data.valid_from == existing.get("valid_from"))
            and (data.valid_to is None or data.valid_to == existing.get("valid_to"))
        ):
            job = db.get_document_job(existing["latest_job_id"])
            if job and (job.get("config") or {}).get("source_url") == source.attribution_url:
                with db.get_connection() as conn:
                    attach_source_response(conn, job["id"], capture)
                return {
                    "document_id": existing["id"], "workflow_id": job["temporal_workflow_id"],
                    "status": job["status"], "source_url": source.attribution_url,
                }
        file = UploadFile(BytesIO(source.content), filename=source.filename)
        try:
            document = await _process_upload(
                user, file, instance, data.kind, code, None,
                json.dumps(crops) if "crop_subjects" in data.model_fields_set else None,
                knowledge_metadata=data.knowledge_metadata.model_dump_json() if data.knowledge_metadata else None,
                valid_from=data.valid_from, valid_to=data.valid_to,
                parser_provider=profile.parser_profile if profile else None, source_url=source.attribution_url,
                source_capture=capture,
            )
        finally:
            await file.close()
        return {
            "document_id": document.id, "workflow_id": document.workflow_id,
            "status": "start_pending" if "retry_upload_start" in document.available_actions else document.stage.value, "source_url": source.attribution_url,
        }

    mapping = CanonicalMapping()
    if isinstance(source, SourceFile) and profile and profile.intake_template:
        try:
            mapping = load_intake_template(profile.intake_template, provider=instance)
        except (OSError, ValueError) as exc:
            raise HTTPException(503, "The configured JSON intake template is unavailable") from exc
    try:
        if isinstance(source, SourceFile):
            payload = decode_source_json(source.content)
            rows = [
                (record, document.model_copy(update={"source_url": source.attribution_url}))
                for record, document in await asyncio.to_thread(map_source_submission, payload, mapping)
            ]
        else:
            document = NormalizedSourceDocument(
                external_id=data.external_id or canonical_json_sha256(data.url), title=source.title,
                text=source.text, language=source.language, kind=data.kind, source_url=source.attribution_url,
                crop_subjects=data.crop_subjects, knowledge_metadata=data.knowledge_metadata,
                valid_from=data.valid_from, valid_to=data.valid_to,
            )
            rows = [(None, document)]
    except (ValidationError, ValueError, RecursionError) as exc:
        raise HTTPException(422, "The downloaded content does not match the knowledge intake schema") from exc

    try:
        with db.get_connection() as conn:
            receipts = []
            for record, document in rows:
                normalized = document.model_dump(mode="json", exclude_none=True)
                normalized_key = canonical_json_sha256(normalized)
                target = upload_release_target(document.kind, provider=instance)
                receipts.append(register_source_document(
                    conn, instance=instance, source_kind="crawl", source_id=source_id,
                    idempotency_key=normalized_key if record is None else crawl_record_key(record, document),
                    raw_payload=normalized if record is None else record,
                    legacy_crawl_key=normalized_key if record is not None else None,
                    document=document, release_target=target,
                ))
                attach_source_response(conn, receipts[-1]["job_id"], capture)
    except IntakeConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, "Provider not found") from exc
    except (ValueError, RecursionError) as exc:
        raise HTTPException(422, "Invalid knowledge metadata or schema attributes") from exc
    if not receipts:
        return {"items": []}
    items = []
    for receipt in receipts:
        started = await start_source_document(receipt)
        items.append({
            "receipt_id": receipt["id"], "document_id": receipt["document_id"], "workflow_id": receipt["workflow_id"],
            "status": "start_pending" if not started else "running" if receipt["status"] == "start_pending" else receipt["status"],
            "source_url": source.attribution_url,
        })
    return items[0] if len(items) == 1 else {"items": items}
