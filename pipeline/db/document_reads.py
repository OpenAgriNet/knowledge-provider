"""Read provider-owned documents and resolve their stable identifiers."""

from typing import Optional

from pipeline.search.index_identity import projection_owner

from . import common, connection

# =============================================================================
# Identifier resolution
# =============================================================================


def _resolve_document_uuid(conn, identifier: Optional[str]) -> Optional[str]:
    """Document UUID for a UUID, Temporal workflow id, fingerprint or filename."""
    if not identifier:
        return None
    identifier = str(identifier)
    identifier = projection_owner(identifier) or identifier

    if common._is_uuid(identifier):
        row = conn.execute("SELECT id FROM documents WHERE id = %s::uuid", (identifier,)).fetchone()
        if row:
            return str(row["id"])

    row = conn.execute(
        """
        SELECT document_id FROM workflows
        WHERE temporal_workflow_id = %s
        ORDER BY started_at DESC LIMIT 1
        """,
        (identifier,),
    ).fetchone()
    if row:
        return str(row["document_id"])

    row = conn.execute(
        """
        SELECT id FROM documents
        WHERE source->>'fingerprint' = %s OR source->>'original_filename' = %s
        ORDER BY created_at DESC LIMIT 1
        """,
        (identifier, identifier),
    ).fetchone()
    if row:
        return str(row["id"])

    row = conn.execute(
        """
        SELECT id FROM documents d
        WHERE EXISTS (
            SELECT 1 FROM jsonb_each(COALESCE(d.metadata->'index_status', '{}'::jsonb)) e
            WHERE e.value->>'doc_id' = %s
        ) OR md5(d.source->>'fingerprint') = %s
        ORDER BY created_at DESC LIMIT 1
        """,
        (identifier, identifier),
    ).fetchone()
    return str(row["id"]) if row else None


def _require_document_uuid(conn, identifier: str) -> str:
    doc_uuid = _resolve_document_uuid(conn, identifier)
    if doc_uuid is None:
        raise LookupError(f"Document not found: {identifier}")
    return doc_uuid


# =============================================================================
# Document row <-> flat dict
# =============================================================================

_DOC_SELECT = """
    SELECT
        d.id, c.id AS catalog_id, d.kind, d.code, d.name, d.aliases, d.source, d.is_disabled, d.metadata,
        d.created_at, d.updated_at,
        g.name AS instance_name,
        (SELECT count(*) FROM pages p WHERE p.document_id = d.id) AS page_total,
        (SELECT count(*) FROM chunks ch WHERE ch.document_id = d.id) AS chunk_total,
        root.temporal_workflow_id AS root_workflow_id,
        root.stop_after_ocr AS root_stop_after_ocr,
        cw.id AS current_workflow_pk,
        cw.current_stage AS current_stage,
        cw.error_message AS current_error_message,
        EXISTS (SELECT 1 FROM workflows w WHERE w.document_id = d.id
                AND w.config->>'release_network_id' IS NOT NULL) AS uses_network_releases,
        u.keycloak_user_id AS up_user_id, u.username AS up_username,
        u.email AS up_email, u.roles AS up_roles,
        ts.ocr_completed_at, ts.translation_completed_at, ts.chunks_completed_at,
        ts.ingested_at,
        req.started_at AS prod_ready_requested_at,
        ru.keycloak_user_id AS prod_ready_requested_by_user_id,
        ru.username AS prod_ready_requested_by_username,
        oa.id AS original_artifact_id,
        na.id AS normalized_artifact_id
    FROM documents d
    LEFT JOIN legacy_document_catalog_types m ON m.kind = d.kind
    LEFT JOIN catalogs c ON c.instance_id = d.provider_id AND c.type = COALESCE(m.catalog_type, d.kind)
    JOIN instances g ON g.id = d.provider_id
    LEFT JOIN document_current_workflow cw ON cw.document_id = d.id
    LEFT JOIN LATERAL (
        SELECT w.temporal_workflow_id, w.stop_after_ocr FROM workflows w
        WHERE w.document_id = d.id
        ORDER BY (w.workflow_type = 'legacy_index_import'), (w.workflow_type IN ('pipeline', 'ocr_only')) DESC, w.started_at ASC, w.id ASC
        LIMIT 1
    ) root ON TRUE
    LEFT JOIN users u ON u.id = NULLIF(d.metadata->>'uploaded_by_user_id', '')::uuid
    LEFT JOIN LATERAL (
        SELECT min(w.stage_timestamps->>'ocr') AS ocr_completed_at,
               min(w.stage_timestamps->>'translation') AS translation_completed_at,
               min(w.stage_timestamps->>'chunks') AS chunks_completed_at,
               max(COALESCE(w.completed_at, w.updated_at))
                   FILTER (WHERE w.current_stage = 'completed') AS ingested_at
        FROM workflows w WHERE w.document_id = d.id
    ) ts ON TRUE
    LEFT JOIN LATERAL (
        SELECT w.started_at, w.started_by_user_id FROM workflows w
        WHERE w.document_id = d.id AND w.workflow_type = 'promote_prod'
          AND w.status = 'waiting_review'
        ORDER BY w.started_at DESC LIMIT 1
    ) req ON TRUE
    LEFT JOIN users ru ON ru.id = req.started_by_user_id
    LEFT JOIN LATERAL (
        SELECT a.id FROM workflow_artifacts a JOIN workflows w ON w.id = a.workflow_id
        WHERE w.document_id = d.id AND a.artifact_type = 'original_upload'
        ORDER BY a.created_at DESC, a.id DESC LIMIT 1
    ) oa ON TRUE
    LEFT JOIN LATERAL (
        SELECT a.id FROM workflow_artifacts a JOIN workflows w ON w.id = a.workflow_id
        WHERE w.document_id = d.id
          AND a.artifact_type IN ('normalized_pdf', 'normalized_spreadsheet')
        ORDER BY a.created_at DESC, a.id DESC LIMIT 1
    ) na ON TRUE
"""


def _document_from_row(row: Optional[dict]) -> Optional[dict]:
    if row is None:
        return None
    source = row["source"] or {}
    meta = row["metadata"] or {}
    fingerprint = source.get("fingerprint")
    roles = row["up_roles"] or []
    entry_aliases = row["aliases"]
    doc = {
        "id": row["id"],
        "workflow_id": row["root_workflow_id"],
        "document_id": fingerprint,
        "canonical_document_id": fingerprint,
        "filename": source.get("original_filename") or "",
        "name": row["name"],
        "source_filename": source.get("original_filename"),
        "source_manifest_name": None,
        "source_file_fingerprint": fingerprint,
        "filepath": source.get("storage_uri") or "",
        "instance": (row["instance_name"] or "").lower(),
        "stage": row["current_stage"] or "registered",
        "page_count": int(row["page_total"] or 0),
        "chunk_count": int(row["chunk_total"] or 0),
        "error_message": row["current_error_message"],
        "is_disabled": bool(row["is_disabled"]),
        "source_type": source.get("source_type"),
        "canonical_input_type": source.get("canonical_input_type"),
        "stop_after_ocr": bool(row["root_stop_after_ocr"]),
        "reindex_required": bool(meta.get("reindex_required")),
        "reindex_reason": meta.get("reindex_reason"),
        "kind": row["kind"],
        "code": row["code"],
        "aliases": list(entry_aliases or []),
        "tool_routing": "qdrant",
        "catalog_visible": bool(meta.get("catalog_visible", True)),
        "network_visible": bool(meta.get("network_visible", True)),
        "uses_network_releases": bool(row["uses_network_releases"]),
        "valid_from": meta.get("valid_from"),
        "valid_to": meta.get("valid_to"),
        "crop_subjects": meta.get("crop_subjects") or [],
        "knowledge_metadata": meta.get("knowledge_metadata") or {},
        "uploaded_by_user_id": row["up_user_id"],
        "uploaded_by_username": row["up_username"],
        "uploaded_by_email": row["up_email"],
        "uploaded_by_roles": ",".join(roles) if roles else None,
        "prod_ready_requested_at": row["prod_ready_requested_at"],
        "prod_ready_requested_by_user_id": row["prod_ready_requested_by_user_id"],
        "prod_ready_requested_by_username": row["prod_ready_requested_by_username"],
        "ocr_completed_at": row["ocr_completed_at"],
        "translation_completed_at": row["translation_completed_at"],
        "chunks_completed_at": row["chunks_completed_at"],
        "ingested_at": row["ingested_at"],
        "original_artifact_id": row["original_artifact_id"],
        "normalized_artifact_id": row["normalized_artifact_id"],
        "latest_job_id": row["current_workflow_pk"],
        "catalog_id": row["catalog_id"],
        "index_status": meta.get("index_status") or {},
        "superseded_doc_ids": meta.get("superseded_doc_ids") or {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    return {k: common._clean(v) for k, v in doc.items()}


def _fetch_document(conn, doc_uuid: Optional[str]) -> Optional[dict]:
    if doc_uuid is None:
        return None
    row = conn.execute(_DOC_SELECT + " WHERE d.id = %s::uuid", (doc_uuid,)).fetchone()
    return _document_from_row(row)


def _document_filters(
    include_disabled: bool,
    instances: Optional[list[str]],
    alias: str = "d",
    instance_alias: str = "g",
) -> tuple[list[str], list]:
    """WHERE clauses for document lists. The query must join ``instances`` as ``instance_alias``."""
    clauses: list[str] = []
    params: list = []
    if not include_disabled:
        clauses.append(f"NOT {alias}.is_disabled")
    if instances is not None:
        clauses.append(f"lower({instance_alias}.name) = ANY(%s)")
        params.append(common._normalize_instances(instances))
    return clauses, params


def owned_source_fingerprint(storage_uri: str, provider: str) -> str | None:
    """Return the single known source fingerprint for an active provider-owned object."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT d.source->>'fingerprint' AS fingerprint
            FROM documents d
            JOIN instances g ON g.id = d.provider_id
            WHERE d.source->>'storage_uri' = %s AND g.name = %s AND NOT d.is_disabled
            LIMIT 2
            """,
            (storage_uri, provider),
        ).fetchall()
    return rows[0]["fingerprint"] if len(rows) == 1 else None


def find_document_by_code(instance: str, kind: str, code: str) -> Optional[dict]:
    """The document holding ``code`` in the instance's ``kind`` catalog, if any."""
    tenant = (instance or common._default_instance()).strip().lower() or common._default_instance()
    with connection.get_connection() as conn:
        row = conn.execute(
            """
            SELECT d.id FROM documents d
            JOIN instances g ON g.id = d.provider_id
            WHERE lower(g.name) = %s AND d.kind = %s AND d.code = %s
            """,
            (tenant, kind, code),
        ).fetchone()
        return _fetch_document(conn, str(row["id"])) if row else None


def get_document(workflow_id: str) -> Optional[dict]:
    """Get a single document by workflow id, UUID or any other identifier."""
    with connection.get_connection() as conn:
        return _fetch_document(conn, _resolve_document_uuid(conn, workflow_id))


def list_documents(
    stage: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    include_disabled: bool = False,
    instances: Optional[list[str]] = None,
) -> list[dict]:
    """List documents with optional stage / instance filters.

    Args:
        stage: Filter by document stage
        limit: Max documents to return
        offset: Pagination offset
        include_disabled: If False (default), excludes soft-deleted documents from results
        instances: If provided, only return docs whose instance is in this list
    """
    if instances is not None and not common._normalize_instances(instances):
        return []
    clauses, params = _document_filters(include_disabled, instances)
    if stage:
        clauses.append("cw.current_stage = %s")
        params.append(stage)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"{_DOC_SELECT} {where} ORDER BY d.created_at DESC, d.id LIMIT %s OFFSET %s",
            (*params, limit, offset),
        ).fetchall()
    return [_document_from_row(r) for r in rows]


_REVIEW_QUEUE_STAGES = ("ocr_review", "translation_review", "chunk_review", "approval_for_prod")


_SUMMARY_KEYS = (
    "total_documents",
    "authoritative_documents",
    "legacy_documents",
    "completed_documents",
    "review_queue",
    "ocr_review_documents",
    "translation_review_documents",
    "chunk_review_documents",
    "translation_processing_documents",
    "chunking_documents",
    "ready_for_ingestion_documents",
    "failed_documents",
    "needs_reindex",
    "running_jobs",
)


def get_document_summary_counts(
    include_disabled: bool = False,
    instances: Optional[list[str]] = None,
) -> dict:
    """Return aggregate document counts for dashboard and migration planning."""
    if instances is not None and not common._normalize_instances(instances):
        return {key: 0 for key in _SUMMARY_KEYS}
    clauses, params = _document_filters(include_disabled, instances)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connection.get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS total_documents,
                0 AS authoritative_documents,
                COUNT(*) AS legacy_documents,
                COUNT(*) FILTER (WHERE cw.current_stage = 'completed') AS completed_documents,
                COUNT(*) FILTER (WHERE cw.current_stage IN
                    ('ocr_review', 'translation_review', 'chunk_review', 'approval_for_prod')) AS review_queue,
                COUNT(*) FILTER (WHERE cw.current_stage = 'ocr_review') AS ocr_review_documents,
                COUNT(*) FILTER (WHERE cw.current_stage = 'translation_review') AS translation_review_documents,
                COUNT(*) FILTER (WHERE cw.current_stage = 'chunk_review') AS chunk_review_documents,
                COUNT(*) FILTER (WHERE cw.current_stage = 'translation_processing') AS translation_processing_documents,
                COUNT(*) FILTER (WHERE cw.current_stage = 'chunking') AS chunking_documents,
                COUNT(*) FILTER (WHERE cw.current_stage = 'ready_for_ingestion') AS ready_for_ingestion_documents,
                COUNT(*) FILTER (WHERE cw.current_stage = 'failed') AS failed_documents,
                COUNT(*) FILTER (WHERE COALESCE((d.metadata->>'reindex_required')::boolean, FALSE)) AS needs_reindex,
                COALESCE(SUM((SELECT COUNT(*) FROM workflows j
                              WHERE j.document_id = d.id AND j.status = 'running')), 0) AS running_jobs
            FROM documents d
            JOIN instances g ON g.id = d.provider_id
            LEFT JOIN document_current_workflow cw ON cw.document_id = d.id
            {where}
            """,
            params,
        ).fetchone()
    return {key: int(row[key] or 0) for key in _SUMMARY_KEYS}


def get_document_counts_by_instance(
    include_disabled: bool = False,
    instances: Optional[list[str]] = None,
) -> list[dict]:
    """Return document counts grouped by instance (state), for dashboard breakdowns."""
    if instances is not None and not common._normalize_instances(instances):
        return []
    clauses, params = _document_filters(include_disabled, instances)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                lower(g.name) AS "instance",
                COUNT(*) AS count,
                COUNT(*) FILTER (WHERE cw.current_stage = 'completed') AS success,
                COUNT(*) FILTER (WHERE cw.current_stage = 'failed') AS failed,
                COUNT(*) FILTER (WHERE cw.current_stage IN
                    ('ocr_review', 'translation_review', 'chunk_review', 'approval_for_prod')) AS dev_approval
            FROM documents d
            JOIN instances g ON g.id = d.provider_id
            LEFT JOIN document_current_workflow cw ON cw.document_id = d.id
            {where}
            GROUP BY lower(g.name)
            ORDER BY count DESC, "instance" ASC
            """,
            params,
        ).fetchall()
    return [
        {
            "instance": row["instance"],
            "count": int(row["count"] or 0),
            "success": int(row["success"] or 0),
            "failed": int(row["failed"] or 0),
            "dev_approval": int(row["dev_approval"] or 0),
        }
        for row in rows
    ]


def get_document_count(stage: Optional[str] = None) -> int:
    """Get count of documents, optionally filtered by stage."""
    with connection.get_connection() as conn:
        if stage:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM documents d
                JOIN document_current_workflow cw ON cw.document_id = d.id
                WHERE cw.current_stage = %s
                """,
                (stage,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM documents").fetchone()
    return int(row["cnt"]) if row else 0


def get_document_source_url(document_id: str) -> str | None:
    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT config->>'source_url' AS url FROM workflows "
            "WHERE document_id = %s::uuid AND config ? 'source_url' "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (document_id,),
        ).fetchone()
    return row["url"] if row else None
