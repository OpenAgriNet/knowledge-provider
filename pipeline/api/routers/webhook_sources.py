"""Provider webhook source policies and authenticated structured document intake."""

import asyncio
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from fastapi.responses import StreamingResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from pipeline import db
from pipeline.api import api as _api
from pipeline.api.routers.providers import ProviderCode, _require_provider_manager
from pipeline.auth import keycloak_admin
from pipeline.auth.config import load_auth_config
from pipeline.auth.deps import CurrentUser, _bearer_token
from pipeline.auth.jwt import decode_verified_claims
from pipeline.auth.oidc import issue_service_token
from pipeline.network.release_policy import index_only_mode, source_release_target
from pipeline.object_storage import ObjectReference, parse_object_uri
from pipeline.intake.source_intake import (
    IntakeConflict,
    canonical_json_sha256,
    get_source_receipt,
    register_source_document,
    start_source_document,
)
from pipeline.intake.webhook_mapping import (
    CANONICAL_SUBMISSION,
    SOURCE_MAPPING,
    CanonicalMapping,
    SourceMapping,
    decode_source_json,
    load_intake_template,
    map_source_submission,
)

router = APIRouter(prefix="/webhook/sources", tags=["webhooks"])
knowledge_router = APIRouter(prefix="/knowledge/intake", tags=["knowledge intake"])

_MAX_BODY_BYTES = 2 * 1024 * 1024
SourceKey = Annotated[str, Path(pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?$")]
_KEY_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", pattern=_KEY_PATTERN)]
DeliveryKey = Annotated[str, Query(pattern=_KEY_PATTERN)]


class SourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_version: int = Field(default=0, ge=0)
    service_client: str = Field(min_length=1, max_length=256)
    service_subject: str | None = Field(default=None, min_length=1, max_length=256)
    mapping: SourceMapping | None = None
    template: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    fixture: dict | list
    enabled: bool = True
    auto_release: bool = False
    release_network_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")


class MappingPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mapping: SourceMapping
    fixture: dict | list


def _resolve_mapping(policy: SourcePolicy, provider: str | None = None) -> SourceMapping:
    if policy.template is None:
        return policy.mapping or CanonicalMapping()
    if policy.mapping is not None:
        raise ValueError("Choose a template or supply a mapping")
    return load_intake_template(policy.template, provider=provider)


@knowledge_router.get("/schema")
def knowledge_intake_schema(user: CurrentUser):
    return CANONICAL_SUBMISSION.json_schema()


def _select_source(conn, provider: str, source: str, *, lock: bool = False) -> dict | None:
    return conn.execute(
        "SELECT s.id::text, s.enabled, s.auto_release, s.release_network_id, s.service_client, s.service_subject, "
        "m.version, m.id::text AS mapping_id, m.mapping, m.mapping_sha256 FROM webhook_sources s "
        "JOIN instances i ON i.id = s.instance_id JOIN webhook_mapping_versions m "
        "ON m.source_id = s.id AND m.version = s.mapping_version "
        "WHERE lower(i.name) = %s AND s.source_key = %s" + (" FOR UPDATE OF s" if lock else ""),
        (provider, source),
    ).fetchone()


@knowledge_router.get("/{provider}")
def list_source_policies(provider: ProviderCode, user: CurrentUser):
    _require_provider_manager(user, provider)
    with db.get_connection() as conn:
        sources = conn.execute(
            "SELECT s.source_key, s.enabled, s.mapping_version AS version FROM webhook_sources s "
            "JOIN instances i ON i.id = s.instance_id WHERE lower(i.name) = %s ORDER BY s.source_key",
            (provider,),
        ).fetchall()
    return {"sources": sources, "index_only": index_only_mode()}


@knowledge_router.post("/{provider}/mapping-preview")
@_api.limiter.limit(_api.RATE_LIMIT_UPLOAD)
async def preview_mapping(provider: ProviderCode, request: Request, user: CurrentUser):
    _require_provider_manager(user, provider)
    try:
        _, payload = await _read_payload(request)
        preview = MappingPreview.model_validate(payload)
        documents = await asyncio.to_thread(map_source_submission, preview.fixture, preview.mapping)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ValidationError):
            issue = exc.errors(include_input=False, include_url=False, include_context=False)[0]
            field = ".".join(map(str, issue["loc"]))
            raise HTTPException(422, f"{field or 'Mapping output'}: {issue['msg']}") from exc
        raise HTTPException(422, "The mapping could not transform this sample.") from exc
    return {"documents": [document.model_dump(mode="json", exclude_none=True) for _, document in documents]}


@knowledge_router.get("/{provider}/{source}/deliveries")
def source_deliveries(provider: ProviderCode, source: SourceKey, user: CurrentUser):
    _require_provider_manager(user, provider)
    with db.get_connection() as conn:
        policy = _select_source(conn, provider, source)
        if not policy:
            raise HTTPException(404, "Webhook source not found")
        deliveries = conn.execute(
            "SELECT d.idempotency_key, d.created_at, m.version, d.raw_storage_uri IS NOT NULL AS has_original, "
            "COALESCE((SELECT jsonb_agg(jsonb_build_object('workflow_id', w.temporal_workflow_id, "
            "'title', r.normalized_document->>'title', 'status', w.status, 'stage', w.current_stage) "
            "ORDER BY receipt.ordinality) "
            "FROM unnest(d.receipt_ids) WITH ORDINALITY receipt(id, ordinality) "
            "JOIN source_intake_receipts r ON r.id = receipt.id "
            "JOIN workflows w ON w.id = r.job_id), '[]'::jsonb) AS documents "
            "FROM webhook_deliveries d JOIN webhook_mapping_versions m ON m.id = d.mapping_version_id "
            "WHERE d.source_id = %s::uuid ORDER BY d.created_at DESC, d.idempotency_key DESC LIMIT 25",
            (policy["id"],),
        ).fetchall()
    return {"deliveries": deliveries}


@knowledge_router.get("/{provider}/{source}/deliveries/original")
def download_delivery(provider: ProviderCode, source: SourceKey, delivery_key: DeliveryKey, user: CurrentUser):
    _require_provider_manager(user, provider)
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT d.raw_storage_uri FROM webhook_deliveries d "
            "JOIN webhook_sources s ON s.id = d.source_id JOIN instances i ON i.id = s.instance_id "
            "WHERE lower(i.name) = %s AND s.source_key = %s AND d.idempotency_key = %s",
            (provider, source, delivery_key),
        ).fetchone()
    if not row or not row["raw_storage_uri"]:
        raise HTTPException(404, "Original delivery body is unavailable")
    response = _api.object_store.open(parse_object_uri(row["raw_storage_uri"]))
    return StreamingResponse(response, media_type="application/json", headers={
        "Content-Disposition": 'attachment; filename="webhook-original.json"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })


@knowledge_router.get("/{provider}/{source}")
@router.get("/{provider}/{source}")
def get_source_policy(provider: ProviderCode, source: SourceKey, user: CurrentUser):
    _require_provider_manager(user, provider)
    with db.get_connection() as conn:
        policy = _select_source(conn, provider, source)
    if not policy:
        raise HTTPException(404, "Webhook source not found")
    return policy


@knowledge_router.put("/{provider}/{source}")
@router.put("/{provider}/{source}")
def put_source_policy(provider: ProviderCode, source: SourceKey, body: SourcePolicy, user: CurrentUser):
    _require_provider_manager(user, provider)
    if body.release_network_id is not None and not body.auto_release:
        raise HTTPException(422, "A release network requires automatic release")
    try:
        selected_mapping = _resolve_mapping(body, provider)
        examples = map_source_submission(body.fixture, selected_mapping)
        if not examples:
            raise ValueError("Fixture must map at least one document")
        if body.enabled and body.auto_release and not index_only_mode():
            for kind in {document.kind for _, document in examples}:
                if not source_release_target(kind, body.release_network_id, provider=provider):
                    raise ValueError("The source release network is unavailable for this document kind")
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(422, "The mapping fixture or release policy is invalid") from exc

    mapping = selected_mapping.model_dump(mode="json", exclude_none=True)
    mapping_hash = canonical_json_sha256(mapping)
    service_subject = keycloak_admin.find_webhook_service_subject(body.service_client) if body.enabled else None
    if service_subject and body.service_subject and service_subject != body.service_subject:
        raise HTTPException(422, "The supplied subject does not belong to this Keycloak client")
    with db.get_connection() as conn:
        instance = conn.execute("SELECT id FROM instances WHERE lower(name) = %s", (provider,)).fetchone()
        if not instance:
            raise HTTPException(404, "Provider not found")
        lock = canonical_json_sha256([provider, source])
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock,))
        prior = _select_source(conn, provider, source, lock=True)
        if (prior["version"] if prior else 0) != body.expected_version:
            raise HTTPException(409, "Webhook source policy changed. Load the current version and retry.")
        if service_subject is None:
            if not prior or body.service_client != prior["service_client"]:
                raise HTTPException(422, "An active webhook source is required before disabling it")
            service_subject = prior["service_subject"]
        version = body.expected_version + 1
        source_id = conn.execute(
            "INSERT INTO webhook_sources (instance_id, source_key, service_client, service_subject, enabled, "
            "auto_release, release_network_id, mapping_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (instance_id, source_key) DO UPDATE SET service_client = excluded.service_client, "
            "service_subject = excluded.service_subject, enabled = excluded.enabled, "
            "auto_release = excluded.auto_release, release_network_id = excluded.release_network_id, "
            "mapping_version = excluded.mapping_version RETURNING id",
            (instance["id"], source, body.service_client, service_subject, body.enabled,
             body.auto_release, body.release_network_id, version),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO webhook_mapping_versions (source_id, version, mapping, mapping_sha256, created_by) "
            "VALUES (%s, %s, %s, %s, %s)", (source_id, version, Jsonb(mapping), mapping_hash, user.user_id),
        )
        return _select_source(conn, provider, source)


def _verified_service_sender(token: str) -> tuple[str, str]:
    config = load_auth_config()
    if not config.keycloak_audience or not config.keycloak_issuer or not config.keycloak_jwks_url:
        raise HTTPException(503, "Webhook Keycloak token validation is not configured")
    claims = decode_verified_claims(token, replace(config, disabled=False))
    client, subject = claims.get("azp"), claims.get("sub")
    if claims.get("typ") != "Bearer" or not isinstance(client, str) or not isinstance(subject, str):
        raise HTTPException(403, "Webhook sender is not authorized")
    return client, subject


async def require_webhook_sender(request: Request) -> tuple[str, str]:
    return await asyncio.to_thread(_verified_service_sender, _bearer_token(request))


class ServiceTokenRequest(BaseModel):
    client_secret: Annotated[SecretStr, Field(min_length=1, max_length=4096)]


@knowledge_router.post("/{provider}/{source}/token")
@_api.limiter.limit(_api.RATE_LIMIT_UPLOAD)
def create_source_token(
    request: Request, response: Response, provider: ProviderCode, source: SourceKey,
    body: ServiceTokenRequest, user: CurrentUser,
):
    _require_provider_manager(user, provider)
    with db.get_connection() as conn:
        policy = _select_source(conn, provider, source)
    if not policy or not policy["enabled"]:
        raise HTTPException(404, "Active webhook source not found")
    token = issue_service_token(policy["service_client"], body.client_secret.get_secret_value())
    if _verified_service_sender(token["access_token"]) != (policy["service_client"], policy["service_subject"]):
        raise HTTPException(403, "Webhook sender is not authorized")
    response.headers["Cache-Control"] = "no-store"
    return token


async def _read_payload(request: Request) -> tuple[bytes, object]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "Webhook events require application/json")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > _MAX_BODY_BYTES:
            raise HTTPException(413, "Webhook body exceeds 2 MiB")
        data.extend(chunk)
    try:
        return bytes(data), decode_source_json(data)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise HTTPException(422, "Invalid webhook JSON") from exc


def _register_delivery(
    provider: str, source: str, key: str, payload: object, raw: bytes, sender: tuple[str, str],
) -> tuple[str, list[dict]]:
    with db.get_connection() as conn:
        policy = _select_source(conn, provider, source, lock=True)
        if not policy or not policy["enabled"] or sender != (policy["service_client"], policy["service_subject"]):
            raise HTTPException(403, "Webhook sender is not authorized")
        digest = canonical_json_sha256(payload)
        prior = conn.execute(
            "SELECT body_sha256, receipt_ids, mapping_version_id::text FROM webhook_deliveries "
            "WHERE source_id = %s::uuid AND idempotency_key = %s", (policy["id"], key),
        ).fetchone()
        if prior:
            if prior["body_sha256"] != digest:
                raise IntakeConflict("Idempotency key was already used for different content")
            receipts = [get_source_receipt(conn, receipt_id) for receipt_id in prior["receipt_ids"]]
            if any(receipt is None for receipt in receipts):
                raise IntakeConflict("The original intake document has been removed")
            return prior["mapping_version_id"], receipts

        if canonical_json_sha256(policy["mapping"]) != policy["mapping_sha256"]:
            raise HTTPException(503, "Webhook mapping integrity check failed")
        mapping = SOURCE_MAPPING.validate_python(policy["mapping"])
        receipts = []
        targets: dict[str, tuple[str, str]] = {}
        release = policy["auto_release"] and not index_only_mode()
        network = policy["release_network_id"] if release else None
        for record, document in map_source_submission(payload, mapping):
            if release and document.kind not in targets:
                try:
                    target = source_release_target(document.kind, network, provider=provider)
                except ValueError as exc:
                    raise HTTPException(503, "Automatic release is unavailable for this source") from exc
                if not target:
                    raise HTTPException(503, "Automatic release is unavailable for this source")
                targets[document.kind] = target
            receipts.append(register_source_document(
                conn, instance=provider, source_kind="webhook", source_id=policy["id"],
                idempotency_key=canonical_json_sha256([key, document.external_id]), raw_payload=record,
                document=document, release_target=targets.get(document.kind), mapping_version_id=policy["mapping_id"],
                explicit_release_network=network is not None,
            ))
        object_key = canonical_json_sha256([key, sha256(raw).hexdigest()])
        original = ObjectReference(_api.OBJECT_STORE_BUCKET, f"webhook-deliveries/{policy['id']}/{object_key}.json")
        try:
            _api.object_store.put(original, BytesIO(raw), len(raw), "application/json")
        except Exception as exc:
            raise HTTPException(503, "The original webhook body could not be stored. Retry with the same key.") from exc
        conn.execute(
            "INSERT INTO webhook_deliveries (source_id, idempotency_key, body_sha256, raw_payload, "
            "mapping_version_id, receipt_ids, raw_storage_uri) "
            "VALUES (%s::uuid, %s, %s, %s, %s::uuid, %s::uuid[], %s)",
            (policy["id"], key, digest, Jsonb(payload), policy["mapping_id"],
             [receipt["id"] for receipt in receipts], original.to_uri()),
        )
        return policy["mapping_id"], receipts


@knowledge_router.post("/{provider}/{source}/submissions", status_code=202)
@router.post("/{provider}/{source}/events", status_code=202)
@_api.limiter.limit(_api.RATE_LIMIT_UPLOAD)
async def receive_event(
    request: Request, provider: ProviderCode, source: SourceKey, idempotency_key: IdempotencyKey,
    sender: Annotated[tuple[str, str], Depends(require_webhook_sender)],
):
    raw, payload = await _read_payload(request)
    try:
        mapping_id, receipts = await asyncio.to_thread(_register_delivery, provider, source, idempotency_key, payload, raw, sender)
    except IntakeConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, RecursionError) as exc:
        raise HTTPException(422, "The webhook payload does not match the source mapping") from exc

    items = []
    for receipt in receipts:
        started = await start_source_document(receipt)
        items.append({
            "receipt_id": receipt["id"], "document_id": receipt["document_id"],
            "workflow_id": receipt["workflow_id"], "status": "accepted" if started else "start_pending",
            "source_url": receipt["source_url"],
        })
    return {"mapping_version_id": mapping_id, "items": items}
