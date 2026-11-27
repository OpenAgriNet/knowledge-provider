"""Durable, bounded polling for deployment-approved knowledge sources."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from io import BytesIO
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

from pipeline import db
from pipeline.intake.source_fetch import SourceFile, fetch_source
from pipeline.intake.source_intake import (
    BinarySource,
    CrawlReleasePolicyConflict,
    NormalizedSourceDocument,
    SourceDocumentMetadata,
    attach_source_response,
    canonical_json_sha256,
    crawl_record_key,
    register_source_document,
    store_source_response,
)
from pipeline.intake.webhook_mapping import (
    CanonicalMapping,
    JsonPointer,
    SourceMapping,
    _read_path,
    decode_source_json,
    load_intake_template,
    map_source_submission,
)
from pipeline.network.release_policy import index_only_mode, source_release_target
from pipeline.object_storage import ObjectReference, load_object_store_config, open_object_store
from pipeline.ocr.service import allowed_ocr_providers

logger = logging.getLogger(__name__)
Code = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]


class PullProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Code
    source_key: Code
    host: str = Field(pattern=r"^[a-zA-Z0-9.-]{1,253}$")
    url: str = Field(min_length=1, max_length=4096)
    interval_seconds: int = Field(default=900, ge=60, le=86400)
    authorization_env: str | None = Field(default=None, pattern=r"^CRAWL_AUTH_[A-Z0-9_]{1,100}$")
    kind: str = "advisory"
    mapping: SourceMapping = Field(default_factory=CanonicalMapping)
    intake_template: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    next_cursor_pointer: JsonPointer | None = None
    cursor_parameter: str = Field(default="cursor", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    auto_release: bool = False
    release_network_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")
    parser_profile: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def resolve_template(self) -> PullProfile:
        if self.release_network_id and not self.auto_release:
            raise ValueError("A release network requires automatic release")
        if self.intake_template is not None:
            if "mapping" in self.model_fields_set:
                raise ValueError("Choose an intake template or supply a mapping")
            self.mapping = load_intake_template(self.intake_template)
        return self

    @property
    def identity(self) -> tuple[str, str, str]:
        profile = self.model_dump(mode="json", exclude={"intake_template"})
        if self.release_network_id is None:
            # Profiles using the default destination retain their existing cursor checkpoint.
            profile.pop("release_network_id")
        return self.provider, self.source_key, canonical_json_sha256(profile)


_PROFILES = TypeAdapter(Annotated[list[PullProfile], Field(max_length=32)])


def bootstrap_profiles() -> list[PullProfile]:
    path = os.environ.get("SCHEDULED_PULL_PROFILES_FILE")
    if not path:
        return []
    with Path(path).open("rb") as handle:
        contents = handle.read(262145)
    if len(contents) > 262144:
        raise ValueError("Scheduled pull configuration exceeds 256 KiB")
    profiles = _PROFILES.validate_python(decode_source_json(contents))
    if len({(profile.provider, profile.source_key) for profile in profiles}) != len(profiles):
        raise ValueError("Scheduled pull profiles must have unique provider and source keys")
    return profiles


def configured_profiles() -> list[PullProfile]:
    from pipeline.intake.provider_sources import all_stored_settings

    settings = all_stored_settings()
    profiles = [profile for profile in bootstrap_profiles() if profile.provider not in settings]
    for provider, stored in settings.items():
        for item in stored["schedules"]:
            item = dict(item)
            name = item.pop("intake_template", None)
            if name:
                item["mapping"] = load_intake_template(name, provider=provider)
            profiles.append(PullProfile.model_validate({**item, "provider": provider}))
    return profiles


def provider_pull_status(provider: str) -> list[dict]:
    profiles = {profile.identity: profile for profile in configured_profiles() if profile.provider == provider}
    if not profiles:
        return []
    with db.get_connection() as conn:
        checkpoints = conn.execute(
            """
            SELECT source_key, profile_sha256, failure_count, next_run_at, last_fetch_at,
                   last_page_policy_conflicts,
                   coalesce(lease_expires_at > now(), false) AS polling
            FROM scheduled_source_pulls
            WHERE provider = %s AND profile_sha256 = ANY(%s)
            """,
            (provider, [profile_hash for _, _, profile_hash in profiles]),
        ).fetchall()
    states = {(row["source_key"], row["profile_sha256"]): row for row in checkpoints}
    sources = []
    for (_, source_key, profile_hash), profile in sorted(profiles.items()):
        row = states.get((source_key, profile_hash), {})
        failures = row.get("failure_count", 0)
        status = "awaiting_worker"
        if row:
            if row["polling"]:
                status = "polling"
            elif failures:
                status = "retrying"
            else:
                status = "scheduled"
        sources.append({
            "source_key": source_key,
            "status": status,
            "auto_release": profile.auto_release,
            "release_network_id": profile.release_network_id,
            "consecutive_failures": failures,
            "last_page_received_at": row.get("last_fetch_at"),
            "last_page_policy_conflicts": row.get("last_page_policy_conflicts", 0),
            "next_check_at": None if row.get("polling") else row.get("next_run_at"),
        })
    return sources


def _claim(profile: PullProfile) -> dict | None:
    with db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO scheduled_source_pulls (provider, source_key, profile_sha256)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
            """, profile.identity,
        )
        return conn.execute(
            """
            UPDATE scheduled_source_pulls
            SET lease_id = %s, lease_expires_at = now() + interval '2 minutes'
            WHERE provider = %s AND source_key = %s AND profile_sha256 = %s
              AND next_run_at <= now() AND (lease_expires_at IS NULL OR lease_expires_at <= now())
            RETURNING cursor, visited_cursors, lease_id, failure_count
            """,
            (uuid4(), *profile.identity),
        ).fetchone()


def _register_page(
    profile: PullProfile, claim: dict, rows: list, next_cursor: str | None, checksum: str,
    binary: BinarySource | None = None, capture: dict | None = None,
) -> bool:
    with db.get_connection() as conn:
        state = conn.execute(
            """
            SELECT lease_id FROM scheduled_source_pulls
            WHERE provider = %s AND source_key = %s AND profile_sha256 = %s AND lease_expires_at > now()
            FOR UPDATE
            """, profile.identity,
        ).fetchone()
        if not state or state["lease_id"] != claim["lease_id"]:
            return False
        targets: dict[str, tuple[str, str]] = {}
        policy_conflicts = 0
        release = profile.auto_release and not index_only_mode()
        for raw, document in rows:
            if release and document.kind not in targets:
                target = source_release_target(document.kind, profile.release_network_id, provider=profile.provider)
                if not target:
                    raise ValueError("Automatic publication is unavailable for this source document")
                targets[document.kind] = target
            try:
                receipt = register_source_document(
                    conn, instance=profile.provider, source_kind="crawl", source_id=profile.source_key,
                    idempotency_key=crawl_record_key(raw, document),
                    raw_payload=raw, document=document, release_target=targets.get(document.kind), binary=binary,
                    explicit_release_network=release and profile.release_network_id is not None,
                )
                if capture:
                    attach_source_response(conn, receipt["job_id"], capture)
            except CrawlReleasePolicyConflict:
                policy_conflicts += 1
        history = [*claim["visited_cursors"], next_cursor] if next_cursor else []
        conn.execute(
            """
            UPDATE scheduled_source_pulls
            SET cursor = %s, visited_cursors = %s, next_run_at = now() + make_interval(secs => %s),
                lease_id = NULL, lease_expires_at = NULL, failure_count = 0, last_error = NULL,
                last_fetch_at = now(), last_payload_sha256 = %s, last_page_policy_conflicts = %s
            WHERE provider = %s AND source_key = %s AND profile_sha256 = %s
            """,
            (next_cursor, history, 0 if next_cursor else profile.interval_seconds, checksum,
             policy_conflicts, *profile.identity),
        )
        return True


def _record_failure(profile: PullProfile, claim: dict, error: Exception) -> None:
    with db.get_connection() as conn:
        conn.execute(
            """
            UPDATE scheduled_source_pulls
            SET failure_count = failure_count + 1, last_error = %s, lease_id = NULL,
                lease_expires_at = NULL, next_run_at = now() + make_interval(secs => %s)
            WHERE provider = %s AND source_key = %s AND profile_sha256 = %s AND lease_id = %s
            """,
            (type(error).__name__, min(3600, 30 * 2 ** min(claim["failure_count"], 7)), *profile.identity, claim["lease_id"]),
        )


def _store_binary(profile: PullProfile, source: SourceFile, checksum: str) -> BinarySource:
    if source.media_type == "application/pdf" and not source.content.startswith(b"%PDF-"):
        raise ValueError("The source did not return a valid PDF header")
    if not source.content:
        raise ValueError("The source returned an empty file")
    if profile.parser_profile and profile.parser_profile not in allowed_ocr_providers():
        raise ValueError("The selected document parser profile is unavailable")
    config = load_object_store_config()
    ref = ObjectReference(
        config.bucket, f"scheduled/{profile.provider}/{profile.source_key}/{checksum}/{source.filename}",
    )
    store = open_object_store(config)
    if config.create_bucket:
        store.ensure_bucket(config.bucket)
    stored_media_type = "application/pdf" if source.media_type == "application/pdf" else "application/octet-stream"
    store.put(ref, BytesIO(source.content), len(source.content), stored_media_type)
    return BinarySource(
        source.filename, ref.to_uri(), hashlib.md5(source.content).hexdigest(),
        stored_media_type, len(source.content), profile.parser_profile,
    )


def _store_response(profile: PullProfile, source) -> dict:
    config = load_object_store_config()
    store = open_object_store(config)
    if config.create_bucket:
        store.ensure_bucket(config.bucket)
    return store_source_response(store, config.bucket, profile.provider, source)


async def poll_source(profile: PullProfile) -> bool:
    claim = await asyncio.to_thread(_claim, profile)
    if not claim:
        return False
    try:
        url = httpx.URL(profile.url)
        if claim["cursor"]:
            url = url.copy_set_param(profile.cursor_parameter, claim["cursor"])
        authorization = os.environ.get(profile.authorization_env) if profile.authorization_env else None
        if profile.authorization_env and not authorization:
            raise ValueError("The scheduled source credential is unavailable")
        source = await fetch_source(str(url), {profile.host.lower()}, authorization)
        capture = await asyncio.to_thread(_store_response, profile, source)
        next_cursor = None
        binary = None
        json_response = isinstance(source, SourceFile) and source.media_type == "application/json"
        if profile.next_cursor_pointer is not None and not json_response:
            raise ValueError("A paginated scheduled feed must return JSON")
        if json_response:
            payload = decode_source_json(source.content)
            checksum = hashlib.sha256(source.content).hexdigest()
            rows = await asyncio.to_thread(map_source_submission, payload, profile.mapping)
            if profile.next_cursor_pointer is not None:
                next_cursor = _read_path(payload, profile.next_cursor_pointer)
                if next_cursor is not None and (
                    not isinstance(next_cursor, str) or not 0 < len(next_cursor) <= 1024
                    or any(ord(ch) < 32 for ch in next_cursor)
                ):
                    raise ValueError("The scheduled source returned an invalid cursor")
        elif isinstance(source, SourceFile):
            checksum = hashlib.sha256(source.content).hexdigest()
            document = SourceDocumentMetadata(
                external_id=profile.source_key, title=Path(source.filename).stem,
                kind=profile.kind, source_url=source.attribution_url,
            )
            binary = await asyncio.to_thread(_store_binary, profile, source, checksum)
            rows = [({"sha256": checksum, "filename": source.filename, "media_type": source.media_type}, document)]
        else:
            document = NormalizedSourceDocument(
                external_id=profile.source_key, title=source.title, text=source.text,
                kind=profile.kind, language=source.language, source_url=source.attribution_url,
            )
            payload = document.model_dump(mode="json")
            rows = [(payload, document)]
            checksum = capture["sha256"]
        if next_cursor and (next_cursor in claim["visited_cursors"] or len(claim["visited_cursors"]) >= 100):
            raise ValueError("The scheduled source repeated a cursor or exceeded the page limit")
        rows = [
            (raw, document.model_copy(update={"source_url": document.source_url or source.attribution_url}))
            for raw, document in rows
        ]
        return await asyncio.to_thread(_register_page, profile, claim, rows, next_cursor, checksum, binary, capture)
    except Exception as error:
        await asyncio.to_thread(_record_failure, profile, claim, error)
        logger.warning("Scheduled pull failed provider=%s source=%s error_type=%s",
                       profile.provider, profile.source_key, type(error).__name__)
        return False


async def poll_due_sources() -> int:
    profiles = configured_profiles()
    slots = asyncio.Semaphore(4)

    async def poll(profile: PullProfile) -> bool:
        async with slots:
            return await poll_source(profile)

    return sum(await asyncio.gather(*(poll(profile) for profile in profiles)))
