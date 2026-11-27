"""Record processing runs and query the operator queues."""

import json
from contextlib import nullcontext
from typing import Optional

from psycopg.types.json import Jsonb

from . import common, connection, document_reads, providers

# =============================================================================
# Workflows (exposed as "jobs")
# =============================================================================

_JOB_SELECT = """
    SELECT w.id, w.document_id, w.workflow_type, w.temporal_workflow_id, w.status,
           w.current_stage, w.config, w.error_message, w.started_at, w.completed_at,
           root.temporal_workflow_id AS doc_workflow_id
    FROM workflows w
    LEFT JOIN LATERAL (
        SELECT r.temporal_workflow_id FROM workflows r
        WHERE r.document_id = w.document_id
        ORDER BY (r.workflow_type = 'legacy_index_import'), (r.workflow_type IN ('pipeline', 'ocr_only')) DESC, r.started_at ASC, r.id ASC
        LIMIT 1
    ) root ON TRUE
"""


def _job_from_row(row: Optional[dict]) -> Optional[dict]:
    if row is None:
        return None
    config = row["config"] or {}
    job = {
        "id": row["id"],
        "workflow_id": row["doc_workflow_id"],
        "job_type": row["workflow_type"],
        "temporal_workflow_id": row["temporal_workflow_id"],
        "temporal_run_id": config.get("temporal_run_id"),
        "status": row["status"],
        "current_stage": row["current_stage"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "error_message": row["error_message"],
        "config": config,
        "config_json": json.dumps(config) if config else None,
    }
    return {k: common._clean(v) for k, v in job.items()}


def root_workflow_id(conn, doc_uuid: str) -> Optional[str]:
    row = conn.execute(
        """
        SELECT temporal_workflow_id FROM workflows WHERE document_id = %s::uuid
        ORDER BY (workflow_type = 'legacy_index_import'), (workflow_type IN ('pipeline', 'ocr_only')) DESC, started_at ASC, id ASC
        LIMIT 1
        """,
        (doc_uuid,),
    ).fetchone()
    return row["temporal_workflow_id"] if row else None


def create_document_job(
    workflow_id: str,
    job_type: str,
    temporal_workflow_id: Optional[str] = None,
    temporal_run_id: Optional[str] = None,
    status: str = "running",
    current_stage: Optional[str] = None,
    config: Optional[dict] = None,
    actor: Optional[dict] = None,
    *,
    _conn=None,
) -> str:
    """Record a workflow run for a document; returns the workflow UUID.

    A ``promote_prod`` run adopts the document's pending request row (see
    ``mark_prod_ready_requested``) instead of adding a second one.
    """
    cfg = dict(config or {})
    if temporal_run_id:
        cfg["temporal_run_id"] = temporal_run_id
    with connection.get_connection() if _conn is None else nullcontext(_conn) as conn:
        doc_uuid = document_reads._require_document_uuid(conn, workflow_id)
        root = root_workflow_id(conn, doc_uuid) or workflow_id
        user_uuid = None
        if actor and actor.get("user_id"):
            user_uuid = providers.upsert_user(
                actor["user_id"],
                actor.get("username"),
                actor.get("email"),
                actor.get("roles"),
                conn=conn,
            )
        if job_type == "promote_prod":
            adopted = conn.execute(
                """
                UPDATE workflows SET temporal_workflow_id = %s, status = %s,
                    current_stage = COALESCE(%s, current_stage),
                    config = config || %s::jsonb
                WHERE id = (
                    SELECT id FROM workflows
                    WHERE document_id = %s::uuid AND workflow_type = 'promote_prod'
                      AND status = 'waiting_review'
                    ORDER BY started_at DESC LIMIT 1)
                RETURNING id
                """,
                (temporal_workflow_id or root, status, current_stage, Jsonb(cfg), doc_uuid),
            ).fetchone()
            if adopted:
                conn.execute(
                    "UPDATE documents SET updated_at = now() WHERE id = %s::uuid", (doc_uuid,)
                )
                return str(adopted["id"])
        row = conn.execute(
            """
            INSERT INTO workflows (document_id, workflow_type, temporal_workflow_id, status,
                                   current_stage, stop_after_ocr, config, started_by_user_id)
            VALUES (%s::uuid, %s, %s, %s,
                    COALESCE(%s, 'registered'), %s, %s, %s::uuid)
            RETURNING id
            """,
            (
                doc_uuid,
                job_type,
                temporal_workflow_id or root,
                status,
                current_stage,
                bool(cfg.get("stop_after_ocr")),
                Jsonb(cfg),
                user_uuid,
            ),
        ).fetchone()
        conn.execute("UPDATE documents SET updated_at = now() WHERE id = %s::uuid", (doc_uuid,))
        return str(row["id"])


def update_document_job(job_id: str, **updates: object) -> Optional[dict]:
    if not updates:
        return get_document_job(job_id)
    if not common._is_uuid(str(job_id)):
        return None
    allowed_fields = {
        "status",
        "current_stage",
        "completed_at",
        "error_message",
        "temporal_run_id",
        "config_json",
    }
    set_clauses = []
    values: list[object] = []
    for key, value in updates.items():
        if key not in allowed_fields:
            continue
        if key == "status":
            set_clauses.append("status = %s")
        elif key == "current_stage":
            set_clauses.append("current_stage = %s")
        elif key == "completed_at":
            set_clauses.append("completed_at = %s::timestamptz")
        elif key == "temporal_run_id":
            set_clauses.append("config = config || jsonb_build_object('temporal_run_id', %s::text)")
        elif key == "config_json":
            if isinstance(value, str):
                value = json.loads(value)
            set_clauses.append("config = config || %s::jsonb")
            value = Jsonb(value or {})
        else:
            set_clauses.append(f"{key} = %s")
        values.append(value)
    if not set_clauses:
        return get_document_job(job_id)
    values.append(str(job_id))
    with connection.get_connection() as conn:
        conn.execute(f"UPDATE workflows SET {', '.join(set_clauses)} WHERE id = %s::uuid", values)
    return get_document_job(job_id)


def mark_upload_started(job_id: str) -> None:
    """Acknowledge Temporal start without overwriting a stage already advanced by the worker."""
    with connection.get_connection() as conn:
        conn.execute(
            "UPDATE workflows SET status = 'running' WHERE id = %s::uuid AND status = 'start_pending'",
            (job_id,),
        )


def get_document_job(job_id: str) -> Optional[dict]:
    if not common._is_uuid(str(job_id)):
        return None
    with connection.get_connection() as conn:
        return _job_from_row(
            conn.execute(_JOB_SELECT + " WHERE w.id = %s::uuid", (str(job_id),)).fetchone()
        )


def get_latest_document_job(workflow_id: str) -> Optional[dict]:
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return None
        return _job_from_row(
            conn.execute(
                _JOB_SELECT
                + " WHERE w.document_id = %s::uuid ORDER BY w.started_at DESC, w.id DESC LIMIT 1",
                (doc_uuid,),
            ).fetchone()
        )


def list_document_jobs(workflow_id: str, limit: int = 50) -> list[dict]:
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return []
        rows = conn.execute(
            _JOB_SELECT
            + " WHERE w.document_id = %s::uuid ORDER BY w.started_at DESC, w.id DESC LIMIT %s",
            (doc_uuid, limit),
        ).fetchall()
    return [_job_from_row(r) for r in rows]


def list_operations_queue(
    limit: int = 100,
    offset: int = 0,
    include_disabled: bool = False,
    instances: Optional[list[str]] = None,
) -> tuple[list[dict], int]:
    """List documents needing action, optionally scoped to tenant instances.

    ``instances``:
      - ``None`` - unrestricted (super admin / bypass)
      - ``[]`` - restricted user with no states -> empty result
      - non-empty - only those document.instance values
    """
    if instances is not None and not common._normalize_instances(instances):
        return [], 0
    clauses, params = document_reads._document_filters(include_disabled, instances)
    clauses.append(
        "(cw.current_stage IN ('ocr_review', 'translation_review', 'chunk_review', "
        "'ready_for_ingestion', 'approval_for_prod', 'failed') "
        "OR COALESCE((d.metadata->>'reindex_required')::boolean, FALSE) "
        "OR cw.status = 'running')"
    )
    where = f"WHERE {' AND '.join(clauses)}"
    from_sql = """
        FROM documents d
        JOIN instances g ON g.id = d.provider_id
        JOIN document_current_workflow cw ON cw.document_id = d.id
    """
    with connection.get_connection() as conn:
        total_row = conn.execute(f"SELECT COUNT(*) AS count {from_sql} {where}", params).fetchone()
        rows = conn.execute(
            f"""
            SELECT
                d.id AS id,
                (SELECT r.temporal_workflow_id FROM workflows r WHERE r.document_id = d.id
                 ORDER BY (r.workflow_type = 'legacy_index_import'), (r.workflow_type IN ('pipeline', 'ocr_only')) DESC, r.started_at ASC, r.id ASC
                 LIMIT 1) AS workflow_id,
                d.source->>'original_filename' AS filename,
                cw.current_stage AS stage,
                lower(g.name) AS "instance",
                cw.error_message,
                COALESCE((d.metadata->>'reindex_required')::boolean, FALSE) AS reindex_required,
                d.metadata->>'reindex_reason' AS reindex_reason,
                cw.id AS job_id,
                cw.workflow_type AS job_type,
                cw.status AS job_status,
                cw.started_at AS started_at
            {from_sql}
            {where}
            ORDER BY cw.started_at DESC, d.id
            LIMIT %s OFFSET %s
            """,
            (*params, limit, offset),
        ).fetchall()
    return [common._clean_row(r) for r in rows], int(total_row["count"] if total_row else 0)


def list_runs(
    limit: int = 100,
    offset: int = 0,
    status: Optional[str] = None,
    instances: Optional[list[str]] = None,
) -> list[dict]:
    """List workflows with document title fields for the Runs UI.

    Args:
        instances: If provided, only return runs whose document instance is in
                   this list. Empty list returns no rows. None = unrestricted.
    """
    where_parts: list[str] = []
    params: list = []
    if status:
        where_parts.append("w.status = %s")
        params.append(status)
    if instances is not None:
        normalized = common._normalize_instances(instances)
        if not normalized:
            return []
        where_parts.append("lower(g.name) = ANY(%s)")
        params.append(normalized)
    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT w.id, w.document_id, w.workflow_type, w.temporal_workflow_id, w.status,
                   w.current_stage, w.config, w.error_message, w.started_at, w.completed_at,
                   (SELECT r.temporal_workflow_id FROM workflows r WHERE r.document_id = w.document_id
                    ORDER BY (r.workflow_type = 'legacy_index_import'), (r.workflow_type IN ('pipeline', 'ocr_only')) DESC, r.started_at ASC, r.id ASC
                    LIMIT 1) AS doc_workflow_id,
                   d.source->>'original_filename' AS filename,
                   d.name AS name,
                   d.source->>'original_filename' AS source_filename,
                   cw.current_stage AS document_stage,
                   lower(g.name) AS "instance"
            FROM workflows w
            JOIN documents d ON d.id = w.document_id
            JOIN instances g ON g.id = d.provider_id
            LEFT JOIN document_current_workflow cw ON cw.document_id = d.id
            {where_sql}
            ORDER BY w.started_at DESC, w.id DESC
            LIMIT %s OFFSET %s
            """,
            (*params, limit, offset),
        ).fetchall()
    result = []
    for row in rows:
        job = _job_from_row(row)
        job["document_uuid"] = str(row["document_id"])
        for extra in ("filename", "name", "source_filename", "document_stage", "instance"):
            job[extra] = row[extra]
        result.append(job)
    return result
