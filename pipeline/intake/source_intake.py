"""Persist source payloads and register document pipeline jobs atomically."""

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from pipeline import db
from pipeline.documents import document_validity
from pipeline.documents.document_kinds import validate_document_kind
from pipeline.metadata.crop_metadata import CropSubjects, parse_crop_subjects
from pipeline.metadata.intake_profiles import apply_intake_profile
from pipeline.metadata.knowledge_metadata import KnowledgeMetadata, LocalizedDocumentMetadata
from pipeline.metadata.localization import normalize_language_tag
from pipeline.network.release_policy import index_only_mode
from pipeline.object_storage import ObjectReference
from pipeline.vector_store import get_default_index_name

logger = logging.getLogger(__name__)


class IntakeConflict(ValueError):
    """The event conflicts with an existing intake receipt or running document."""


class CrawlReleasePolicyConflict(IntakeConflict):
    """An unchanged crawl item already has a receipt under another release policy."""


class SourceDocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    external_id: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=256)
    kind: str = Field(default="advisory", validate_default=True)
    source_url: str | None = Field(default=None, max_length=2048)
    crop_subjects: CropSubjects = Field(default_factory=list)
    knowledge_metadata: KnowledgeMetadata | None = None
    validity: document_validity.TimePeriod | None = None
    valid_from: str | None = Field(default=None, exclude=True)
    valid_to: str | None = Field(default=None, exclude=True)

    @field_validator("kind")
    @classmethod
    def check_kind(cls, value: str) -> str:
        return validate_document_kind(value)

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, source_url: str | None) -> str | None:
        if source_url is not None:
            parsed = urlsplit(source_url)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None:
                raise ValueError("source_url must be an HTTP(S) URL without credentials")
        return source_url

    @model_validator(mode="after")
    def validate_metadata(self) -> "SourceDocumentMetadata":
        if self.valid_from or self.valid_to:
            if "validity" in self.model_fields_set:
                raise ValueError("Supply one validity period")
            self.validity = document_validity.read_validity({
                "valid_from": self.valid_from, "valid_to": self.valid_to,
            })
            self.valid_from = self.valid_to = None
        parse_crop_subjects(json.dumps([crop.model_dump(exclude_none=True) for crop in self.crop_subjects]))
        return self

    def with_intake_profile(self):
        metadata = self.knowledge_metadata.storage_value() if self.knowledge_metadata else None
        validated = apply_intake_profile(self.kind, metadata)
        if validated is None or validated == metadata:
            return self
        return self.model_copy(update={"knowledge_metadata": KnowledgeMetadata.model_validate(validated)})


TranslationText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=250_000)]


class SourceTranslation(LocalizedDocumentMetadata):
    text: TranslationText


class NormalizedSourceDocument(SourceDocumentMetadata):
    text: str = Field(min_length=1, max_length=250_000)
    language: str | None = None
    translations: dict[str, TranslationText | SourceTranslation] = Field(default_factory=dict, max_length=12)

    @field_validator("language")
    @classmethod
    def validate_language(cls, language: str | None) -> str | None:
        normalized = normalize_language_tag(language)
        if language is not None and normalized is None:
            raise ValueError("language must be a BCP 47 tag")
        return normalized

    @field_validator("translations")
    @classmethod
    def validate_translations(cls, entries: dict[str, str | SourceTranslation]) -> dict[str, str | SourceTranslation]:
        variants = {}
        for language, translation in entries.items():
            tag = normalize_language_tag(language)
            if tag is None or tag in variants:
                raise ValueError("translations must have unique language tags")
            variants[tag] = translation
        return variants

    @model_validator(mode="after")
    def validate_translation_metadata(self) -> "NormalizedSourceDocument":
        if self.language in self.translations:
            raise ValueError("translations must use a different language from the source")
        self.knowledge_metadata = self.metadata_with_translation_labels(self.knowledge_metadata)
        return self

    def metadata_with_translation_labels(self, metadata: KnowledgeMetadata | None) -> KnowledgeMetadata | None:
        labels = {
            tag: translation.model_dump(exclude={"text"}, exclude_none=True)
            for tag, translation in self.translations.items()
            if isinstance(translation, SourceTranslation) and (translation.title or translation.description)
        }
        if not labels:
            return metadata
        merged = metadata.storage_value() if metadata else {}
        localized = merged.setdefault("localized", {})
        for tag, fields in labels.items():
            existing = localized.setdefault(tag, {})
            if any(field in existing and existing[field] != value for field, value in fields.items()):
                raise ValueError("Translation labels conflict with localized knowledge metadata")
            existing.update(fields)
        return KnowledgeMetadata.model_validate(merged)


@dataclass(frozen=True)
class BinarySource:
    filename: str
    storage_uri: str
    fingerprint: str
    mime_type: str
    size_bytes: int
    parser_profile: str | None = None


def store_source_response(store, bucket: str, provider: str, source) -> dict:
    """Keep fetched bytes private, separate from processed/downloadable artifacts."""
    checksum = hashlib.sha256(source.content).hexdigest()
    # Neither a URL, credential, nor a provider-supplied filename enters the key.
    provider_key = hashlib.sha256(provider.encode()).hexdigest()[:32]
    ref = ObjectReference(bucket, f"private/source-responses/{provider_key}/{checksum}")
    store.put(ref, BytesIO(source.content), len(source.content), "application/octet-stream")
    return {
        "storage_uri": ref.to_uri(), "sha256": checksum,
        "media_type": source.media_type, "size_bytes": len(source.content),
    }


def attach_source_response(conn, job_id: str, capture: dict) -> None:
    """Retain all observed byte variants without exposing them as artifacts."""
    conn.execute(
        "UPDATE workflows SET config = jsonb_set(config, '{private_source_responses}', "
        "COALESCE(config->'private_source_responses', '{}'::jsonb) || %s::jsonb) WHERE id = %s::uuid",
        (Jsonb({capture["sha256"]: capture}), job_id),
    )


def public_source_job(job: dict) -> dict:
    """Remove private intake evidence from both job API representations."""
    result = dict(job)
    for key in ("config", "config_json"):
        value = result.get(key)
        if not value:
            continue
        parsed = json.loads(value) if isinstance(value, str) else dict(value)
        parsed.pop("private_source_responses", None)
        result[key] = json.dumps(parsed) if isinstance(value, str) else parsed
    return result


def canonical_json_sha256(payload: object) -> str:
    pending = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("Source payload cannot contain null characters")
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def crawl_record_key(record: object, document: SourceDocumentMetadata) -> str:
    return canonical_json_sha256([record, _source_semantics(document)])


def _source_semantics(document: SourceDocumentMetadata) -> dict:
    values = document.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    if values.get("knowledge_metadata"):
        values["knowledge_metadata"].pop("attribute_profile", None)
        if not values["knowledge_metadata"]:
            values.pop("knowledge_metadata")
    return values


def get_source_receipt(conn, receipt_id: str) -> dict | None:
    return conn.execute(
        "SELECT r.id::text, r.document_id::text, r.job_id::text, r.external_id, "
        "r.mapping_version_id::text, r.normalized_document->>'source_url' AS source_url, "
        "w.status, w.config, w.temporal_workflow_id AS workflow_id "
        "FROM source_intake_receipts r JOIN workflows w ON w.id = r.job_id WHERE r.id = %s::uuid",
        (receipt_id,),
    ).fetchone()


def _replay_source_receipt(
    conn, receipt_id: str, source_kind: str, release_target: tuple[str, str] | None,
    explicit_release_network: bool,
    *,
    verified_raw: bool = True,
) -> dict:
    receipt = get_source_receipt(conn, receipt_id)
    if not receipt:
        raise IntakeConflict("The original intake document has been removed")
    if source_kind == "crawl":
        config = receipt["config"]
        network = config.get("release_network_id")
        args = config.get("upload_start_args") or []
        collection = config.get("release_collection") or (args[6] if len(args) > 6 else None)
        original_target = (network, collection) if network else None
        if original_target != release_target or bool(config.get("explicit_release_network")) != explicit_release_network:
            conflict = CrawlReleasePolicyConflict if verified_raw else IntakeConflict
            raise conflict("The existing crawl uses a different release policy. Select its revision for the intended network.")
    return receipt


def register_source_document(
    conn, *, instance: str, source_kind: str, source_id: str, idempotency_key: str,
    raw_payload: object, document: SourceDocumentMetadata, release_target: tuple[str, str] | None,
    mapping_version_id: str | None = None, binary: BinarySource | None = None,
    legacy_crawl_key: str | None = None,
    explicit_release_network: bool = False,
) -> dict:
    """Create one document job, or a revision for an existing source record."""
    instance_row = conn.execute("SELECT id FROM instances WHERE lower(name) = %s", (instance,)).fetchone()
    if not instance_row:
        raise LookupError("Provider not found")
    instance_id = instance_row["id"]
    # Serialize this source across retries, including two deliveries with different keys.
    lock = canonical_json_sha256([str(instance_id), source_kind, source_id])
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock,))
    raw_sha = canonical_json_sha256(raw_payload)
    scope = (instance_id, source_kind, source_id)
    previous = conn.execute(
        "SELECT id::text, raw_sha256 FROM source_intake_receipts WHERE instance_id = %s AND source_kind = %s "
        "AND source_id = %s AND idempotency_key = %s", (*scope, idempotency_key),
    ).fetchone()
    if previous:
        if previous["raw_sha256"] != raw_sha:
            raise IntakeConflict("Idempotency key was already used for different content")
        return _replay_source_receipt(conn, previous["id"], source_kind, release_target, explicit_release_network)

    if source_kind == "crawl":
        # Model defaults and canonical date names can change across releases.
        # Retain a verified prior receipt when the source and mapped values agree.
        with conn.cursor(name="receipt_replay_" + uuid4().hex) as candidates:
            candidates.itersize = 64
            candidates.execute(
                "SELECT id::text, normalized_document, raw_sha256, raw_payload "
                "FROM source_intake_receipts WHERE instance_id = %s AND source_kind = %s AND source_id = %s "
                "AND external_id = %s ORDER BY created_at DESC, id DESC",
                (*scope, document.external_id),
            )
            for candidate in candidates:
                try:
                    source_model = SourceDocumentMetadata if binary else NormalizedSourceDocument
                    previous_document = source_model.model_validate(candidate["normalized_document"])
                    if _source_semantics(previous_document) != _source_semantics(document):
                        continue
                    raw_matches = candidate["raw_sha256"] == raw_sha
                    if not raw_matches:
                        previous_raw = candidate["raw_payload"]
                        if (_source_semantics(source_model.model_validate(previous_raw)) != _source_semantics(previous_document)
                                or _source_semantics(source_model.model_validate(raw_payload)) != _source_semantics(document)):
                            continue
                except ValueError:
                    continue
                return _replay_source_receipt(
                    conn, candidate["id"], source_kind, release_target, explicit_release_network, verified_raw=raw_matches,
                )

    if source_kind == "crawl":
        observed = conn.execute(
            "SELECT id::text, replay_evidence_sha256 FROM source_intake_receipts "
            "WHERE instance_id = %s AND source_kind = %s AND source_id = %s AND replay_evidence_key = %s",
            (*scope, idempotency_key),
        ).fetchone()
        if observed:
            if observed["replay_evidence_sha256"] != raw_sha:
                raise IntakeConflict("Idempotency key was already used for different content")
            return _replay_source_receipt(conn, observed["id"], source_kind, release_target, explicit_release_network)

    if source_kind == "crawl" and legacy_crawl_key:
        # Retain the first observed item alongside the original receipt payload.
        previous = conn.execute(
            "SELECT id::text FROM source_intake_receipts WHERE instance_id = %s AND source_kind = %s "
            "AND source_id = %s AND idempotency_key = %s AND raw_sha256 = %s "
            "AND external_id = %s AND raw_payload = normalized_document "
            "AND replay_evidence_key IS NULL",
            (*scope, legacy_crawl_key, legacy_crawl_key, document.external_id),
        ).fetchone()
        if previous:
            receipt = _replay_source_receipt(
                conn, previous["id"], source_kind, release_target, explicit_release_network, verified_raw=False,
            )
            conn.execute(
                "UPDATE source_intake_receipts SET replay_evidence_key = %s, replay_evidence_payload = %s, "
                "replay_evidence_sha256 = %s, replay_evidence_observed_at = now() WHERE id = %s",
                (idempotency_key, Jsonb(raw_payload), raw_sha, previous["id"]),
            )
            return receipt

    prior_document = conn.execute(
        "SELECT document_id::text, normalized_document->>'kind' AS kind FROM source_intake_receipts "
        "WHERE instance_id = %s AND source_kind = %s "
        "AND source_id = %s AND external_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
        (*scope, document.external_id),
    ).fetchone()
    receipt_id = str(uuid4())
    workflow_id = f"intake-{receipt_id}"
    if binary is None and not isinstance(document, NormalizedSourceDocument):
        raise ValueError("A text submission must contain its normalized text")
    filename = binary.filename if binary else f"{source_kind}-{receipt_id}.json"
    filepath = binary.storage_uri if binary else f"intake://{receipt_id}"
    fingerprint = binary.fingerprint if binary else raw_sha[:32]
    canonical_input_type = "structured_text"
    if binary:
        canonical_input_type = "spreadsheet" if Path(binary.filename).suffix.lower() in {".csv", ".xlsx"} else "pdf"
    document = document.with_intake_profile()
    normalized = document.model_dump(mode="json", exclude_none=True)
    crops = [crop.model_dump(exclude_none=True) for crop in document.crop_subjects]
    knowledge_metadata = document.knowledge_metadata.storage_value() if document.knowledge_metadata else {}
    validity = document_validity.storage_value(document.validity)
    if prior_document:
        if (prior_document["kind"] or "advisory") != document.kind:
            raise IntakeConflict("An existing source document cannot change its knowledge kind")
        document_id = prior_document["document_id"]
        job = conn.execute(
            "SELECT id::text FROM document_current_workflow WHERE document_id = %s::uuid", (document_id,),
        ).fetchone()
        if not job:
            raise IntakeConflict("Existing document has no workflow to replace")
        try:
            db.reset_document_for_reupload(
                document_id, filename=filename, filepath=filepath, fingerprint=fingerprint,
                source_type=source_kind, canonical_input_type=canonical_input_type, crop_subjects=crops,
                knowledge_metadata=knowledge_metadata,
                expected_job_id=job["id"], _conn=conn,
            )
        except db.DocumentReplacementConflict as exc:
            raise IntakeConflict(str(exc)) from exc
        db.update_document_fields(
            document_id, name=document.title, validity=validity, _conn=conn,
        )
    else:
        stable_code = canonical_json_sha256([source_id, document.external_id])[:32]
        document_id = db.upsert_document(
            workflow_id, fingerprint, filename, filepath, name=document.title, source_type=source_kind,
            canonical_input_type=canonical_input_type, instance=instance, validity=document.validity,
            kind=document.kind, code=f"{source_kind}-{stable_code}",
            crop_subjects=crops, knowledge_metadata=knowledge_metadata, with_workflow=False, _conn=conn,
        )
        if validity is None:
            db.update_document_fields(document_id, validity=None, _conn=conn)

    network, collection = release_target or (None, get_default_index_name())
    args = [fingerprint, filename, filepath, 450, 128, 1, collection, True, False, True]
    parser = binary.parser_profile if binary else None
    if network or parser or index_only_mode():
        args.append(network)
    if parser or index_only_mode():
        args.append(parser)
    if index_only_mode():
        args.append(True)
    job_id = db.create_document_job(
        document_id, job_type="pipeline", temporal_workflow_id=workflow_id, status="start_pending",
        current_stage="registered", config={
            "upload_start_args": args, "release_network_id": network, "release_collection": collection if network else None,
            "explicit_release_network": explicit_release_network,
            "auto_approve": True,
            "source_kind": source_kind, "source_id": source_id, "source_receipt_id": receipt_id,
            "mapping_version_id": mapping_version_id,
            "source_url": document.source_url,
            "source_task_queue": os.environ.get("TEMPORAL_TASK_QUEUE", "ocr-pipeline"),
        }, _conn=conn,
    )
    if binary:
        db.add_document_artifact(
            workflow_id=workflow_id, job_id=job_id, artifact_type="original_upload",
            storage_uri=filepath, stage="registered", filename=filename, mime_type=binary.mime_type,
            size_bytes=binary.size_bytes, metadata={"source_kind": source_kind}, _conn=conn,
        )
    conn.execute(
        "INSERT INTO source_intake_receipts (id, instance_id, source_kind, source_id, idempotency_key, "
        "external_id, raw_payload, raw_sha256, normalized_document, mapping_version_id, document_id, job_id) "
        "VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid, %s::uuid, %s::uuid)",
        (receipt_id, *scope, idempotency_key, document.external_id, Jsonb(raw_payload), raw_sha, Jsonb(normalized),
         mapping_version_id, document_id, job_id),
    )
    return get_source_receipt(conn, receipt_id)


async def _dispatch_source_document(receipt: dict, client, task_queue: str) -> bool:
    """Start a committed intent using its original idempotent Temporal ID."""
    if receipt["status"] != "start_pending":
        return True
    if client is None:
        return False
    from pipeline.documents.document_dispatch import start_registered_document

    try:
        await start_registered_document(client, task_queue, {
            "id": receipt["job_id"], "config": receipt["config"], "temporal_workflow_id": receipt["workflow_id"],
        })
    except Exception as exc:
        logger.warning("Source intake start pending workflow_id=%s error=%s", receipt["workflow_id"], type(exc).__name__)
        return False
    return True


async def start_source_document(receipt: dict) -> bool:
    from pipeline.api import api

    queue = receipt["config"].get("source_task_queue") or api.TASK_QUEUE
    return await _dispatch_source_document(receipt, api.temporal_client, queue)


def _claim_pending_sources(batch_size: int, task_queue: str) -> list[dict]:
    with db.get_connection() as conn:
        return conn.execute(
            "WITH pending AS ("
            "SELECT w.id, COALESCE((w.config->>'source_dispatch_attempts')::int, 0) AS attempts "
            "FROM workflows w JOIN source_intake_receipts r ON r.job_id = w.id "
            "WHERE w.status = 'start_pending' AND r.created_at <= now() - interval '5 seconds' "
            "AND COALESCE(w.config->>'source_task_queue', 'ocr-pipeline') = %s "
            "AND COALESCE((w.config->>'source_next_dispatch_at')::timestamptz, now()) <= now() "
            "ORDER BY r.created_at, w.id LIMIT %s FOR UPDATE OF w SKIP LOCKED"
            ") UPDATE workflows w SET config = w.config || jsonb_build_object("
            "'source_dispatch_attempts', least(p.attempts + 1, 10), "
            "'source_next_dispatch_at', now() + make_interval(secs => least(300, 10 * power(2, p.attempts)))) "
            "FROM pending p WHERE w.id = p.id "
            "RETURNING w.id::text AS job_id, w.status, w.config, w.temporal_workflow_id AS workflow_id",
            (task_queue, batch_size),
        ).fetchall()


async def reconcile_pending_sources(client, task_queue: str, *, batch_size: int = 16, concurrency: int = 4) -> int:
    """Retry due source intents with bounded concurrency and durable backoff."""
    pending = await asyncio.to_thread(_claim_pending_sources, max(1, min(batch_size, 64)), task_queue)
    slots = asyncio.Semaphore(max(1, min(concurrency, 8)))

    async def dispatch(receipt: dict) -> bool:
        async with slots:
            return await _dispatch_source_document(receipt, client, task_queue)

    return sum(await asyncio.gather(*(dispatch(receipt) for receipt in pending)))


def materialize_intake_pages(workflow_id: str, filepath: str) -> dict:
    """Read a receipt only when it belongs to the workflow that is running it."""
    receipt_id = str(UUID(filepath.removeprefix("intake://")))
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT r.normalized_document FROM source_intake_receipts r JOIN workflows w ON w.id = r.job_id "
            "JOIN document_current_workflow c ON c.id = w.id "
            "WHERE r.id = %s::uuid AND w.temporal_workflow_id = %s",
            (receipt_id, workflow_id),
        ).fetchone()
    if not row:
        raise LookupError("No intake receipt for the current workflow")
    db.save_pages(workflow_id, [submitted_document_page(row["normalized_document"])])
    return {"page_count": 1, "source_receipt_id": receipt_id}


def submitted_document_page(document: dict) -> dict:
    return {
        "page_number": 1, "original_markdown": document["text"],
        "detected_language": document.get("language"),
        "metadata": {"declared_language": document.get("language")},
        "translations": {
            tag: {
                "translated_markdown": variant if isinstance(variant, str) else variant["text"],
                "provider": "source_intake",
            }
            for tag, variant in document.get("translations", {}).items()
        },
    }
