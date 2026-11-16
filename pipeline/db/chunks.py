"""Persist, edit and retrieve document passages."""

import json
from typing import Any, Optional

from psycopg.types.json import Jsonb

from pipeline.documents.models import (
    ChunkingRun,
)

from . import common, connection, document_reads, jobs

# =============================================================================
# Chunk Functions
# =============================================================================

_CHUNK_SELECT = """
    SELECT c.id, c.document_id, c.chunk_number, c.original_text, c.edited_text, c.token_count,
           c.page_start, c.page_end, c.source_spans, c.section_title, c.content_type,
           c.is_reference, c.is_reviewed, c.is_excluded, c.reviewer_notes, c.tags,
           c.created_at, c.updated_at, d.metadata->'chunking' AS chunking
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
"""


def _chunk_from_row(row: Optional[dict], workflow_id: str) -> Optional[dict]:
    if row is None:
        return None
    spans = row["source_spans"] or []
    page_numbers = sorted({int(s["page_number"]) for s in spans if "page_number" in s}) or list(
        range(int(row["page_start"]), int(row["page_end"]) + 1)
    )
    run = row.get("chunking") or {}
    chunk = {
        "id": row["id"],
        "workflow_id": workflow_id,
        "chunk_number": row["chunk_number"],
        "original_text": row["original_text"],
        "edited_text": row["edited_text"],
        "token_count": row["token_count"],
        "page_start": row["page_start"],
        "page_end": row["page_end"],
        "source_page_numbers_json": json.dumps(page_numbers),
        "source_spans_json": json.dumps(spans),
        "section_title": row["section_title"],
        "content_type": row["content_type"],
        "is_reference": bool(row["is_reference"]),
        "chunking_provider": run.get("provider"),
        "chunking_model": run.get("model"),
        "chunking_config_json": json.dumps(run["config"]) if run.get("config") else None,
        "chunking_run_id": f"run-{run['run_number']}" if run.get("run_number") else None,
        "chunk_version": run.get("run_number") or 1,
        "is_reviewed": bool(row["is_reviewed"]),
        "is_excluded": bool(row["is_excluded"]),
        "reviewer_notes": row["reviewer_notes"],
        "tags": row.get("tags") or [],
    }
    return {k: common._clean(v) for k, v in chunk.items()}


def _load_json_value(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def save_chunks(workflow_id: str, chunks: list[dict]):
    """
    Save all chunks for a document.

    Re-chunking runs in one transaction: the document's chunks are deleted, the
    new ones inserted, and ``metadata.chunking`` (with ``run_number``
    incremented) updated together.
    """
    with connection.get_connection() as conn:
        doc_uuid = document_reads._require_document_uuid(conn, workflow_id)
        previous = conn.execute(
            "SELECT metadata->'chunking'->>'run_number' AS run_number FROM documents "
            "WHERE id = %s::uuid FOR UPDATE",
            (doc_uuid,),
        ).fetchone()
        run_number = int(previous["run_number"] or 0) + 1
        conn.execute("DELETE FROM chunks WHERE document_id = %s::uuid", (doc_uuid,))
        for chunk in chunks:
            spans = _load_json_value(
                chunk.get("source_spans")
                if chunk.get("source_spans") is not None
                else chunk.get("source_spans_json"),
                [],
            )
            page_start = int(chunk.get("page_start") or 1)
            page_end = max(int(chunk.get("page_end") or page_start), page_start)
            conn.execute(
                """
                INSERT INTO chunks (document_id, chunk_number, original_text, edited_text,
                                    token_count, page_start, page_end, source_spans,
                                    section_title, content_type, is_reference, is_reviewed,
                                    is_excluded, reviewer_notes)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    doc_uuid,
                    chunk.get("chunk_number"),
                    chunk.get("original_text") or "",
                    chunk.get("edited_text"),
                    chunk.get("token_count") or 0,
                    page_start,
                    page_end,
                    Jsonb(spans),
                    chunk.get("section_title"),
                    chunk.get("content_type"),
                    bool(chunk.get("is_reference")),
                    bool(chunk.get("is_reviewed")),
                    bool(chunk.get("is_excluded")),
                    chunk.get("reviewer_notes"),
                ),
            )
        meta_patch: dict[str, Any] = {}
        if chunks:
            first = chunks[0]
            meta_patch["chunking"] = ChunkingRun(
                run_number=run_number,
                provider=first.get("chunking_provider"),
                model=first.get("chunking_model"),
                config=_load_json_value(first.get("chunking_config_json"), {}),
                created_at=common._iso_now(),
            ).model_dump()
        if meta_patch:
            conn.execute(
                "UPDATE documents SET metadata = metadata || %s::jsonb WHERE id = %s::uuid",
                (Jsonb(meta_patch), doc_uuid),
            )
        else:
            conn.execute("UPDATE documents SET updated_at = now() WHERE id = %s::uuid", (doc_uuid,))


def get_chunks(workflow_id: str, include_excluded: bool = False) -> list[dict]:
    """Get all chunks for a document."""
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return []
        root = jobs.root_workflow_id(conn, doc_uuid) or workflow_id
        excluded = "" if include_excluded else "AND NOT c.is_excluded"
        rows = conn.execute(
            f"{_CHUNK_SELECT} WHERE c.document_id = %s::uuid {excluded} ORDER BY c.chunk_number",
            (doc_uuid,),
        ).fetchall()
    return [_chunk_from_row(r, root) for r in rows]


def search_chunks(
    *,
    query: str = "",
    limit: int = 100,
    offset: int = 0,
    include_excluded: bool = False,
    stage: Optional[str] = None,
    instances: Optional[list[str]] = None,
) -> tuple[list[dict], int]:
    """Search chunks across documents for maintainer workflows.

    instances: If provided, only return chunks whose owning document's instance
    is in this list (tenant scoping). ``None`` means no instance restriction.
    """
    where_clauses = ["TRUE"]
    params: list = []

    if not include_excluded:
        where_clauses.append("NOT c.is_excluded")
    if stage:
        where_clauses.append("cw.current_stage = %s")
        params.append(stage)
    if instances is not None:
        normalized = common._normalize_instances(instances)
        if not normalized:
            where_clauses.append("FALSE")
        else:
            where_clauses.append("lower(g.name) = ANY(%s)")
            params.append(normalized)
    if query and query.strip():
        where_clauses.append("lower(coalesce(c.edited_text, c.original_text)) LIKE %s")
        params.append(f"%{query.strip().lower()}%")

    base_from_sql = f"""
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        JOIN instances g ON g.id = d.provider_id
        LEFT JOIN document_current_workflow cw ON cw.document_id = d.id
        WHERE {" AND ".join(where_clauses)}
    """
    with connection.get_connection() as conn:
        total_row = conn.execute(f"SELECT COUNT(*) AS cnt {base_from_sql}", params).fetchone()
        total = int(total_row["cnt"] if total_row and total_row["cnt"] is not None else 0)
        rows = conn.execute(
            f"""
            SELECT
                c.id, c.chunk_number, c.original_text, c.edited_text, c.token_count,
                c.page_start, c.page_end, c.source_spans, c.section_title, c.content_type,
                c.is_reference, c.is_reviewed, c.is_excluded, c.reviewer_notes, c.tags,
                d.metadata->'chunking' AS chunking,
                d.source->>'fingerprint' AS document_id,
                d.source->>'original_filename' AS filename,
                d.name AS name,
                cw.current_stage AS stage,
                d.updated_at AS document_updated_at,
                (SELECT r.temporal_workflow_id FROM workflows r WHERE r.document_id = d.id
                 ORDER BY (r.workflow_type = 'legacy_index_import'), (r.workflow_type IN ('pipeline', 'ocr_only')) DESC, r.started_at ASC, r.id ASC
                 LIMIT 1) AS root_workflow_id
            {base_from_sql}
            ORDER BY d.updated_at DESC, d.id, c.chunk_number
            LIMIT %s OFFSET %s
            """,
            [*params, int(limit), int(offset)],
        ).fetchall()

    chunks: list[dict] = []
    for row in rows:
        row = dict(row)
        chunk = _chunk_from_row(row, row["root_workflow_id"])
        for extra in ("document_id", "filename", "name", "stage", "document_updated_at"):
            chunk[extra] = common._clean(row[extra])
        chunks.append(chunk)
    return chunks, total


def get_chunk(workflow_id: str, chunk_num: int) -> Optional[dict]:
    """Get a specific chunk."""
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return None
        root = jobs.root_workflow_id(conn, doc_uuid) or workflow_id
        row = conn.execute(
            _CHUNK_SELECT + " WHERE c.document_id = %s::uuid AND c.chunk_number = %s",
            (doc_uuid, chunk_num),
        ).fetchone()
    return _chunk_from_row(row, root)


def update_chunk(
    workflow_id: str,
    chunk_num: int,
    edited_text: Optional[str] = None,
    is_reviewed: Optional[bool] = None,
    is_excluded: Optional[bool] = None,
    reviewer_notes: Optional[str] = None,
    tags: list[str] | None = None,
) -> Optional[dict]:
    """Update a chunk. Returns updated chunk or None if not found."""
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return None
        existing = conn.execute(
            "SELECT id FROM chunks WHERE document_id = %s::uuid AND chunk_number = %s",
            (doc_uuid, chunk_num),
        ).fetchone()
        if not existing:
            return None

        updates = []
        values: list = []
        if edited_text is not None:
            updates.append("edited_text = %s")
            values.append(edited_text)
        if is_reviewed is not None:
            updates.append("is_reviewed = %s")
            values.append(bool(is_reviewed))
        if is_excluded is not None:
            updates.append("is_excluded = %s")
            values.append(bool(is_excluded))
        if reviewer_notes is not None:
            updates.append("reviewer_notes = %s")
            values.append(reviewer_notes)
        if tags is not None:
            updates.append("tags = %s")
            distinct = {}
            for tag in tags:
                distinct.setdefault(tag.casefold(), tag)
            values.append(list(distinct.values()))
        if updates:
            conn.execute(
                f"UPDATE chunks SET {', '.join(updates)} WHERE document_id = %s::uuid AND chunk_number = %s",
                (*values, doc_uuid, chunk_num),
            )
    return get_chunk(workflow_id, chunk_num)


def reset_chunk(workflow_id: str, chunk_num: int) -> Optional[dict]:
    """Reset a chunk to its original text."""
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is not None:
            conn.execute(
                """
                UPDATE chunks SET edited_text = NULL, is_reviewed = FALSE, is_excluded = FALSE,
                                  reviewer_notes = NULL
                WHERE document_id = %s::uuid AND chunk_number = %s
                """,
                (doc_uuid, chunk_num),
            )
    return get_chunk(workflow_id, chunk_num)
