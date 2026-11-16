"""Create, replace and update document records."""

from contextlib import nullcontext
from datetime import date
from typing import Any, Optional

import psycopg
from psycopg.types.json import Jsonb

from pipeline.documents.models import (
    DocumentMetadata,
    DocumentSource,
)
from pipeline.metadata.schema_packs import validate_schema_selection
from pipeline.search.index_identity import projection_doc_id

from . import common, connection, document_reads, jobs, providers


class DuplicateDocumentCode(ValueError):
    """The ``(catalog, code)`` pair is already held by another document."""


class DocumentReplacementConflict(ValueError):
    """The document changed while a replacement upload was being registered."""


# =============================================================================
# Documents
# =============================================================================


def _stored_source(conn, doc_uuid: str) -> dict:
    row = conn.execute("SELECT source FROM documents WHERE id = %s::uuid", (doc_uuid,)).fetchone()
    return dict(row["source"]) if row else {}


def upsert_document(
    workflow_id: str,
    document_id: str,
    filename: str,
    filepath: str,
    canonical_document_id: Optional[str] = None,
    name: Optional[str] = None,
    source_filename: Optional[str] = None,
    source_manifest_name: Optional[str] = None,
    source_file_fingerprint: Optional[str] = None,
    stage: str = "registered",
    page_count: int = 0,
    chunk_count: int = 0,
    error_message: Optional[str] = None,
    ocr_completed_at: Optional[str] = None,
    translation_completed_at: Optional[str] = None,
    chunks_completed_at: Optional[str] = None,
    ingested_at: Optional[str] = None,
    source_type: Optional[str] = None,
    canonical_input_type: Optional[str] = None,
    stop_after_ocr: bool = False,
    original_artifact_id: Optional[str] = None,
    normalized_artifact_id: Optional[str] = None,
    latest_job_id: Optional[str] = None,
    instance: Optional[str] = None,
    uploaded_by_user_id: Optional[str] = None,
    uploaded_by_username: Optional[str] = None,
    uploaded_by_email: Optional[str] = None,
    uploaded_by_roles: Optional[str] = None,
    valid_from: Optional[str] = None,
    valid_to: Optional[str] = None,
    with_workflow: bool = True,
    kind: Optional[str] = None,
    code: Optional[str] = None,
    aliases: Optional[list[str]] = None,
    crop_subjects: Optional[list[dict]] = None,
    knowledge_metadata: Optional[dict] = None,
    *,
    _conn=None,
) -> str:
    """Insert or update a document record; returns the document UUID.

    A new document needs a kind and a code unique within its provider and kind.
    The instance code resolves to the provider UUID. A missing display name
    defaults to the code. Existing workflows keep their original provider and
    kind when re-registering a document.
    Uploader identity is set on INSERT, and filled on UPDATE only when currently null.

    ``valid_from``/``valid_to`` are the document's validity period, stored as
    given and likewise INSERT-only, so an approver's later edit survives a
    restart that re-registers the same workflow. What the period should default
    to is the caller's call (see ``pipeline/document_validity.py``); omitting
    both stores NULL, which search reads as "no stated period".

    ``page_count`` and ``chunk_count`` are accepted for call-site compatibility
    and ignored: both are counted from ``pages`` and ``chunks``.

    A new document also gets its first workflow row (``temporal_workflow_id`` =
    ``workflow_id``) unless ``with_workflow`` is False, for callers that create
    it themselves with ``create_document_job`` (addressing the document by the
    returned UUID, since no workflow id resolves to it yet).
    """
    instance_value = (
        instance or common._default_instance()
    ).strip().lower() or common._default_instance()
    fingerprint = common._fingerprint_for(
        source_file_fingerprint or canonical_document_id, document_id, workflow_id
    )
    source_patch = {
        "fingerprint": fingerprint,
        "original_filename": filename,
        "storage_uri": filepath,
        "source_type": source_type,
        "canonical_input_type": canonical_input_type,
    }
    with connection.get_connection() if _conn is None else nullcontext(_conn) as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id) if workflow_id else None
        if doc_uuid:
            patch = {k: v for k, v in source_patch.items() if v is not None}
            DocumentSource.model_validate({**_stored_source(conn, doc_uuid), **patch})
            uploader_uuid = (
                providers.upsert_user(
                    uploaded_by_user_id,
                    uploaded_by_username,
                    uploaded_by_email,
                    uploaded_by_roles,
                    conn=conn,
                    tenants=[instance_value],
                )
                if uploaded_by_user_id
                else None
            )
            conn.execute(
                """
                UPDATE documents SET
                    source = source || %s::jsonb,
                    name = COALESCE(%s, name),
                    metadata = CASE
                        WHEN %s::text IS NULL OR metadata ? 'uploaded_by_user_id' THEN metadata
                        ELSE metadata || jsonb_build_object('uploaded_by_user_id', %s::text)
                    END
                WHERE id = %s::uuid
                """,
                (Jsonb(patch), name or None, uploader_uuid, uploader_uuid, doc_uuid),
            )
            cw = conn.execute(
                "SELECT id FROM document_current_workflow WHERE document_id = %s::uuid", (doc_uuid,)
            ).fetchone()
            if cw:
                conn.execute(
                    """
                    UPDATE workflows SET current_stage = %s, error_message = %s,
                        stop_after_ocr = %s
                    WHERE id = %s::uuid
                    """,
                    (stage, error_message, bool(stop_after_ocr), str(cw["id"])),
                )
            return doc_uuid

        if not kind or not code:
            raise ValueError("A new document needs kind and code")
        source = DocumentSource.model_validate(
            {k: v for k, v in source_patch.items() if v is not None}
        ).model_dump(exclude_none=True)
        user_uuid = (
            providers.upsert_user(
                uploaded_by_user_id,
                uploaded_by_username,
                uploaded_by_email,
                uploaded_by_roles,
                conn=conn,
                tenants=[instance_value],
            )
            if uploaded_by_user_id
            else None
        )
        validated_metadata = DocumentMetadata(
            valid_from=valid_from,
            valid_to=valid_to,
            uploaded_by_user_id=user_uuid,
            crop_subjects=crop_subjects or [],
            knowledge_metadata=knowledge_metadata,
        )
        metadata = validated_metadata.model_dump(mode="json", exclude_none=True)
        if validated_metadata.knowledge_metadata is not None:
            metadata["knowledge_metadata"] = validated_metadata.knowledge_metadata.storage_value()
            validate_schema_selection(conn, metadata["knowledge_metadata"])
        provider_id = providers.get_or_create_instance(instance_value, conn=conn)
        try:
            with conn.transaction():
                row = conn.execute(
                    """
                    INSERT INTO documents (provider_id, kind, code, name, aliases, source, metadata)
                    VALUES (%s::uuid, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        provider_id,
                        kind,
                        code,
                        name or code,
                        [a for a in (aliases or []) if a],
                        Jsonb(source),
                        Jsonb(metadata),
                    ),
                ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name == "documents_provider_kind_code_key":
                raise DuplicateDocumentCode(
                    f"Code '{code}' is already used in the {kind} catalog of {instance_value}"
                ) from exc
            raise
        if with_workflow:
            conn.execute(
                """
                INSERT INTO workflows (document_id, workflow_type, temporal_workflow_id,
                                       current_stage, error_message, stop_after_ocr,
                                       started_by_user_id)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s::uuid)
                """,
                (
                    str(row["id"]),
                    "ocr_only" if stop_after_ocr else "pipeline",
                    workflow_id,
                    stage,
                    error_message,
                    bool(stop_after_ocr),
                    user_uuid,
                ),
            )
        return str(row["id"])


def reset_document_for_reupload(
    doc_uuid: str,
    *,
    filename: str,
    filepath: str,
    fingerprint: str,
    source_type: Optional[str] = None,
    canonical_input_type: Optional[str] = None,
    crop_subjects: Optional[list[dict]] = None,
    knowledge_metadata: Optional[dict] = None,
    expected_job_id: Optional[str] = None,
    _conn=None,
) -> None:
    """Point an existing document at a re-uploaded file.

    ``source`` is replaced, pages and chunks are cleared for the new run, and
    the document is flagged ``reindex_required`` because its vector index is now
    stale. Earlier workflows and artifacts stay as history.

    The fingerprint is the document's key in the vector index, so the old key
    is remembered in ``metadata.superseded_doc_ids`` for every index (dev/prod)
    that holds it. The next ingest/promote and disable/purge delete those points;
    without this they would stay searchable with nothing pointing at them.
    """
    source = DocumentSource.model_validate(
        {
            "fingerprint": fingerprint,
            "original_filename": filename,
            "storage_uri": filepath,
            "source_type": source_type,
            "canonical_input_type": canonical_input_type,
        }
    ).model_dump(exclude_none=True)
    crop_patch = (
        {
            "crop_subjects": DocumentMetadata(crop_subjects=crop_subjects).model_dump(
                exclude_none=True
            )["crop_subjects"]
        }
        if crop_subjects is not None
        else {}
    )
    if knowledge_metadata is not None:
        crop_patch["knowledge_metadata"] = DocumentMetadata(
            knowledge_metadata=knowledge_metadata
        ).knowledge_metadata.storage_value()
    with connection.get_connection() if _conn is None else nullcontext(_conn) as conn:
        validate_schema_selection(conn, crop_patch.get("knowledge_metadata"))
        current = conn.execute(
            "SELECT source->>'fingerprint' AS fingerprint, metadata, is_disabled FROM documents "
            "WHERE id = %s::uuid FOR UPDATE",
            (doc_uuid,),
        ).fetchone()
        if current is None:
            raise LookupError(f"Document not found: {doc_uuid}")
        if expected_job_id:
            job = conn.execute(
                "SELECT id::text, status FROM document_current_workflow WHERE document_id = %s::uuid",
                (doc_uuid,),
            ).fetchone()
            if (
                current["is_disabled"]
                or not job
                or job["id"] != expected_job_id
                or job["status"]
                not in {
                    "completed",
                    "failed",
                    "cancelled",
                }
            ):
                raise DocumentReplacementConflict(
                    "This document changed before the replacement could start"
                )
        meta = current["metadata"] or {}
        superseded = {k: list(v) for k, v in (meta.get("superseded_doc_ids") or {}).items()}
        for env, entry in (meta.get("index_status") or {}).items():
            old_key = (entry or {}).get("doc_id") or current["fingerprint"]
            current_keys = {fingerprint, projection_doc_id({"id": doc_uuid}, fingerprint)}
            if old_key and old_key not in current_keys and old_key not in superseded.get(env, []):
                superseded.setdefault(env, []).append(old_key)
        conn.execute("DELETE FROM pages WHERE document_id = %s::uuid", (doc_uuid,))
        conn.execute("DELETE FROM chunks WHERE document_id = %s::uuid", (doc_uuid,))
        conn.execute(
            """
            UPDATE documents SET
                source = %s::jsonb,
                metadata = (metadata - 'chunking')
                    || jsonb_build_object('reindex_required', TRUE, 'reindex_reason', 'source_replaced',
                                          'superseded_doc_ids', %s::jsonb)
                    || %s::jsonb
            WHERE id = %s::uuid
            """,
            (Jsonb(source), Jsonb(superseded), Jsonb(crop_patch), doc_uuid),
        )


def set_document_disabled(workflow_id: str, is_disabled: bool = True):
    """Soft delete a document (filtered from all queries by default).

    Unlike hard delete, this preserves the document record for audit purposes.
    Use X-Include-Disabled: true header in API to see disabled documents.
    """
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid:
            conn.execute(
                "UPDATE documents SET is_disabled = %s WHERE id = %s::uuid",
                (bool(is_disabled), doc_uuid),
            )


def delete_document(workflow_id: str):
    """Delete a document record and everything that cascades from it."""
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid:
            conn.execute("DELETE FROM documents WHERE id = %s::uuid", (doc_uuid,))


def purge_document(workflow_id: str, keep_audit: bool = True) -> dict:
    """Hard delete every row belonging to a document.

    Unlike :func:`set_document_disabled` this is irreversible - there is
    nothing left for ``POST /documents/{id}/restore`` to bring back.

    Audit rows are kept by default: they are the record that the deletion
    happened, not part of the document itself. Their ``document_id`` becomes
    NULL; the ``workflow_id`` and ``instance`` snapshots stay.

    Returns a per-table count of deleted rows.
    """
    deleted: dict[str, int] = {}
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return deleted
        root = jobs.root_workflow_id(conn, doc_uuid) or workflow_id

        def _count(sql: str) -> int:
            return int(conn.execute(sql, (doc_uuid,)).fetchone()["n"])

        counts = {
            "chunks": _count("SELECT count(*) AS n FROM chunks WHERE document_id = %s::uuid"),
            "pages": _count("SELECT count(*) AS n FROM pages WHERE document_id = %s::uuid"),
            "document_artifacts": _count(
                "SELECT count(*) AS n FROM workflow_artifacts a JOIN workflows w ON w.id = a.workflow_id "
                "WHERE w.document_id = %s::uuid"
            ),
            "document_jobs": _count(
                "SELECT count(*) AS n FROM workflows WHERE document_id = %s::uuid"
            ),
        }
        if not keep_audit:
            row = conn.execute(
                "DELETE FROM audit_logs WHERE document_id = %s::uuid OR workflow_id = %s",
                (doc_uuid, root),
            )
            if row.rowcount > 0:
                deleted["audit_logs"] = row.rowcount
        conn.execute("DELETE FROM documents WHERE id = %s::uuid", (doc_uuid,))
        for table, count in counts.items():
            if count > 0:
                deleted[table] = count
        deleted["documents"] = 1
    return deleted


def update_document_fields(workflow_id: str, *, _conn=None, **updates: object) -> Optional[dict]:
    """Update selected document fields and return the document.

    Flat names are mapped onto the tables: ``stage``/``error_message`` act on the
    current workflow, ``code``/``name``/``aliases`` on the document columns,
    a document's kind is fixed by its catalog at upload, and flags,
    period and uploader go into ``metadata``. Names derived from other rows (counts, artifact and job
    pointers, completion timestamps) are accepted and ignored.
    """
    with connection.get_connection() if _conn is None else nullcontext(_conn) as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return None
        if not updates:
            return document_reads._fetch_document(conn, doc_uuid)

        doc_sets: list[str] = []
        doc_params: list = []
        source_patch: dict[str, Any] = {}
        meta_patch: dict[str, Any] = {}
        meta_remove: list[str] = []
        wf_sets: list[str] = []
        wf_params: list = []

        for key, value in updates.items():
            if key == "stage":
                wf_sets.append("current_stage = %s")
                wf_params.append(value)
            elif key == "error_message":
                wf_sets.append("error_message = %s")
                wf_params.append(value)
            elif key == "source_type":
                source_patch["source_type"] = value
            elif key == "canonical_input_type":
                source_patch["canonical_input_type"] = value
            elif key == "filepath":
                source_patch["storage_uri"] = value
            elif key in ("filename", "source_filename"):
                if key == "filename" or "filename" not in updates:
                    source_patch["original_filename"] = value
            elif key in ("document_id", "canonical_document_id", "source_file_fingerprint"):
                if isinstance(value, str) and common._FINGERPRINT_RE.match(value):
                    source_patch["fingerprint"] = value
            elif key == "name":
                if value:
                    doc_sets.append("name = %s")
                    doc_params.append(value)
            elif key == "code":
                if value:
                    doc_sets.append("code = %s")
                    doc_params.append(value)
            elif key == "aliases":
                doc_sets.append("aliases = %s")
                doc_params.append([a for a in (value or []) if a])
            elif key in ("reindex_required", "catalog_visible", "network_visible"):
                meta_patch[key] = bool(value)
            elif key == "reindex_reason":
                if value:
                    meta_patch["reindex_reason"] = value
                else:
                    meta_remove.append("reindex_reason")
            elif key == "stop_after_ocr":
                conn.execute(
                    "UPDATE workflows SET stop_after_ocr = %s WHERE id = ("
                    "SELECT id FROM workflows WHERE document_id = %s::uuid "
                    "ORDER BY (workflow_type = 'legacy_index_import'), (workflow_type IN ('pipeline', 'ocr_only')) DESC, started_at ASC, id ASC LIMIT 1)",
                    (bool(value), doc_uuid),
                )
            elif key in ("valid_from", "valid_to"):
                meta_patch[key] = value.isoformat() if isinstance(value, date) else value
            elif key == "uploaded_by_user_id" and value:
                has_uploader = conn.execute(
                    "SELECT metadata ? 'uploaded_by_user_id' AS has FROM documents WHERE id = %s::uuid",
                    (doc_uuid,),
                ).fetchone()["has"]
                if not has_uploader:
                    meta_patch["uploaded_by_user_id"] = providers.upsert_user(
                        str(value),
                        updates.get("uploaded_by_username"),
                        updates.get("uploaded_by_email"),
                        updates.get("uploaded_by_roles"),
                        conn=conn,
                    )

        if source_patch:
            doc_sets.append("source = source || %s::jsonb")
            doc_params.append(Jsonb({k: v for k, v in source_patch.items() if v is not None}))
        if meta_patch or meta_remove:
            expr = "metadata"
            for removed in meta_remove:
                expr = f"({expr} - '{removed}')"
            if meta_patch:
                expr = f"{expr} || %s::jsonb"
                doc_params.append(Jsonb(meta_patch))
            doc_sets.append(f"metadata = {expr}")

        if wf_sets:
            conn.execute(
                f"UPDATE workflows SET {', '.join(wf_sets)} WHERE id = ("
                "SELECT id FROM document_current_workflow WHERE document_id = %s::uuid)",
                (*wf_params, doc_uuid),
            )
        if doc_sets:
            try:
                with conn.transaction():
                    conn.execute(
                        f"UPDATE documents SET {', '.join(doc_sets)} WHERE id = %s::uuid",
                        (*doc_params, doc_uuid),
                    )
            except psycopg.errors.UniqueViolation as exc:
                if exc.diag.constraint_name == "documents_provider_kind_code_key":
                    raise DuplicateDocumentCode(
                        "Another document in the target catalog already uses this code"
                    ) from exc
                raise
        elif wf_sets:
            conn.execute("UPDATE documents SET updated_at = now() WHERE id = %s::uuid", (doc_uuid,))
        return document_reads._fetch_document(conn, doc_uuid)


def mark_document_reindex_required(
    workflow_id: str,
    required: bool = True,
    reason: Optional[str] = None,
) -> Optional[dict]:
    return update_document_fields(
        workflow_id,
        reindex_required=bool(required),
        reindex_reason=reason if required else None,
    )


def set_document_validity(
    workflow_id: str,
    valid_from: str,
    valid_to: str,
) -> Optional[dict]:
    """Record the period this document's chunks are searchable in. Both ends
    are `YYYY-MM-DD`; validation belongs to the caller
    (`pipeline/document_validity.parse_period`), not to this SQL layer."""
    return update_document_fields(workflow_id, valid_from=valid_from, valid_to=valid_to)


def mark_prod_ready_requested(
    workflow_id: str,
    user_id: Optional[str] = None,
    username: Optional[str] = None,
) -> Optional[dict]:
    """Flag that a state_admin has asked a super_admin to review this document
    for prod promotion. Purely informational - does not fire the approve_prod
    signal itself; the super_admin still makes that call separately.

    Recorded as a ``promote_prod`` workflow in ``waiting_review``."""
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return None
        root = jobs.root_workflow_id(conn, doc_uuid) or workflow_id
        user_uuid = (
            providers.upsert_user(user_id, username, None, None, conn=conn) if user_id else None
        )
        conn.execute(
            """
            UPDATE workflows SET status = 'running'
            WHERE document_id = %s::uuid AND workflow_type = 'promote_prod' AND status = 'waiting_review'
            """,
            (doc_uuid,),
        )
        conn.execute(
            """
            INSERT INTO workflows (document_id, workflow_type, temporal_workflow_id, status,
                                   current_stage, started_by_user_id)
            VALUES (%s::uuid, 'promote_prod', %s, 'waiting_review', 'approval_for_prod', %s::uuid)
            """,
            (doc_uuid, f"{root}-prod-request", user_uuid),
        )
        return document_reads._fetch_document(conn, doc_uuid)


def clear_prod_ready_requested(workflow_id: str) -> Optional[dict]:
    """Reset the request flag once prod approval actually happens (or the
    document leaves the approval_for_prod gate for any other reason)."""
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return None
        conn.execute(
            """
            UPDATE workflows SET status = 'running'
            WHERE document_id = %s::uuid AND workflow_type = 'promote_prod' AND status = 'waiting_review'
            """,
            (doc_uuid,),
        )
        return document_reads._fetch_document(conn, doc_uuid)
