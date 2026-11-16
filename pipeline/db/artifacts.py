"""Store references to workflow artifacts in object storage."""

import json
from contextlib import nullcontext
from typing import Optional

from psycopg.types.json import Jsonb

from . import common, connection, document_reads

# =============================================================================
# Artifacts
# =============================================================================


def _artifact_from_row(row: Optional[dict]) -> Optional[dict]:
    if row is None:
        return None
    metadata = row["metadata"] or {}
    artifact = {
        "id": row["id"],
        "workflow_id": row["doc_workflow_id"],
        "job_id": row["workflow_pk"],
        "artifact_type": row["artifact_type"],
        "stage": row["stage"],
        "storage_uri": row["storage_uri"],
        "mime_type": row["mime_type"],
        "filename": row["filename"],
        "size_bytes": row["size_bytes"],
        "metadata_json": json.dumps(metadata) if metadata else None,
        "created_at": row["created_at"],
    }
    return {k: common._clean(v) for k, v in artifact.items()}


_ARTIFACT_SELECT = """
    SELECT a.id, a.workflow_id AS workflow_pk, a.artifact_type, a.stage, a.storage_uri,
           a.mime_type, a.filename, a.size_bytes, a.metadata, a.created_at,
           (SELECT r.temporal_workflow_id FROM workflows r WHERE r.document_id = w.document_id
            ORDER BY (r.workflow_type = 'legacy_index_import'), (r.workflow_type IN ('pipeline', 'ocr_only')) DESC, r.started_at ASC, r.id ASC
            LIMIT 1) AS doc_workflow_id
    FROM workflow_artifacts a
    JOIN workflows w ON w.id = a.workflow_id
"""


def add_document_artifact(
    workflow_id: str,
    artifact_type: str,
    storage_uri: str,
    stage: Optional[str] = None,
    job_id: Optional[str] = None,
    mime_type: Optional[str] = None,
    filename: Optional[str] = None,
    size_bytes: Optional[int] = None,
    metadata: Optional[dict] = None,
    *,
    artifact_id: str | None = None,
    _conn=None,
) -> str:
    """Record a file produced by a workflow; returns the artifact UUID.

    Belongs to ``job_id`` when given, else to the document's current workflow.
    """
    with connection.get_connection() if _conn is None else nullcontext(_conn) as conn:
        doc_uuid = document_reads._require_document_uuid(conn, workflow_id)
        wf_uuid = None
        if job_id and common._is_uuid(str(job_id)):
            row = conn.execute(
                "SELECT id FROM workflows WHERE id = %s::uuid AND document_id = %s::uuid",
                (str(job_id), doc_uuid),
            ).fetchone()
            wf_uuid = str(row["id"]) if row else None
        if wf_uuid is None:
            row = conn.execute(
                "SELECT id FROM document_current_workflow WHERE document_id = %s::uuid", (doc_uuid,)
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    INSERT INTO workflows (document_id, workflow_type, temporal_workflow_id)
                    VALUES (%s::uuid, 'pipeline', %s) RETURNING id
                    """,
                    (doc_uuid, workflow_id),
                ).fetchone()
            wf_uuid = str(row["id"])
        created = conn.execute(
            """
            INSERT INTO workflow_artifacts (id, workflow_id, artifact_type, stage, storage_uri,
                                            mime_type, filename, size_bytes, metadata)
            VALUES (COALESCE(%s::uuid, gen_random_uuid()), %s::uuid, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """,
            (
                artifact_id,
                wf_uuid,
                artifact_type,
                stage,
                storage_uri,
                mime_type,
                filename,
                size_bytes,
                Jsonb(metadata or {}),
            ),
        ).fetchone()
        return str(created["id"]) if created else artifact_id


def list_document_artifacts(workflow_id: str) -> list[dict]:
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return []
        rows = conn.execute(
            _ARTIFACT_SELECT
            + " WHERE w.document_id = %s::uuid ORDER BY a.created_at DESC, a.id DESC",
            (doc_uuid,),
        ).fetchall()
    return [_artifact_from_row(r) for r in rows]


def get_document_artifact(workflow_id: str, artifact_id: str) -> Optional[dict]:
    if not common._is_uuid(str(artifact_id)):
        return None
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return None
        return _artifact_from_row(
            conn.execute(
                _ARTIFACT_SELECT + " WHERE w.document_id = %s::uuid AND a.id = %s::uuid",
                (doc_uuid, str(artifact_id)),
            ).fetchone()
        )
