"""Persist per-document index state and enforce search visibility."""

import json
from typing import Optional

from psycopg.types.json import Jsonb

from pipeline.documents.models import (
    IndexState,
)
from pipeline.search.index_identity import projection_owner

from . import chunks as chunk_queries
from . import common, connection, document_reads, jobs


def index_key_for(index_name: str) -> str:
    return index_name


def doc_id_used_by_other_document(doc_id: str, exclude_doc_uuid: Optional[str], env: str) -> bool:
    """True when another *live* document is indexed in ``env`` (dev/prod) under ``doc_id``.

    The index key is the file fingerprint, so two documents holding the same file
    share points in the vector index. Deleting a key another document still needs
    would remove that document's vectors too. Only a document that is not
    disabled and whose own ``index_status[env]`` holds this key counts: one that
    was merely uploaded, or is disabled, has nothing in the index to protect.
    """
    with connection.get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM documents d
            WHERE (%s::uuid IS NULL OR d.id <> %s::uuid)
              AND NOT d.is_disabled
              AND d.metadata->'index_status'->%s::text->>'doc_id' = %s
            LIMIT 1
            """,
            (exclude_doc_uuid, exclude_doc_uuid, env, doc_id),
        ).fetchone()
    return row is not None


def clear_superseded_doc_ids(workflow_id: str, env: str, doc_ids: list[str]) -> None:
    """Forget superseded index keys once their points are deleted from ``env`` (dev/prod)."""
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return
        row = conn.execute(
            "SELECT metadata->'superseded_doc_ids' AS sup FROM documents WHERE id = %s::uuid FOR UPDATE",
            (doc_uuid,),
        ).fetchone()
        superseded = {k: list(v) for k, v in (row["sup"] or {}).items()}
        remaining = [i for i in superseded.get(env, []) if i not in set(doc_ids)]
        if remaining:
            superseded[env] = remaining
        else:
            superseded.pop(env, None)
        conn.execute(
            "UPDATE documents SET metadata = metadata || jsonb_build_object('superseded_doc_ids', %s::jsonb) "
            "WHERE id = %s::uuid",
            (Jsonb(superseded), doc_uuid),
        )


def visible_document_hits(hits: list[dict], *, index_name: str) -> list[dict]:
    """Require a successful serving projection and PostgreSQL passage ownership."""
    keys = list({str(hit.get("doc_id")) for hit in hits if hit.get("doc_id")})
    if not keys:
        return []
    with connection.get_connection() as conn:
        documents = conn.execute(
            """
            SELECT d.id::text, i.name AS instance, state.value->>'doc_id' AS indexed_key,
                   cw.id::text AS current_workflow_id, cw.temporal_workflow_id,
                   frozen.workflow_id::text AS frozen_workflow_id, frozen.passages
            FROM documents d JOIN instances i ON i.id = d.provider_id
            CROSS JOIN LATERAL jsonb_each(COALESCE(d.metadata->'index_status', '{}'::jsonb)) state
            LEFT JOIN document_current_workflow cw ON cw.document_id = d.id
            LEFT JOIN LATERAL (
                SELECT r.workflow_id, r.snapshot->'passages' AS passages
                FROM document_revisions r JOIN workflows w ON w.id = r.workflow_id
                WHERE r.document_id = d.id AND (
                    r.snapshot->>'vector_doc_id' = state.value->>'doc_id'
                    OR concat('document:', d.id, ':', r.id) = state.value->>'doc_id'
                    OR (w.status = 'completed' AND concat('document:', d.id, ':', r.snapshot->'source'->>'source_file_fingerprint') = state.value->>'doc_id')
                )
                ORDER BY (r.snapshot->>'vector_doc_id' = state.value->>'doc_id') DESC NULLS LAST, r.created_at DESC
                LIMIT 1
            ) frozen ON TRUE
            WHERE NOT d.is_disabled AND state.value->>'index_name' = %s
              AND state.value->>'status' = 'indexed' AND state.value->>'doc_id' = ANY(%s::text[])
            """,
            (index_name, keys),
        ).fetchall()
        document_ids = list({row["id"] for row in documents})
        chunks = conn.execute(
            "SELECT document_id::text, chunk_number, is_excluded, COALESCE(edited_text, original_text) AS text FROM chunks "
            "WHERE document_id = ANY(%s::uuid[])",
            (document_ids,),
        ).fetchall()
    from pipeline.search.passage_projection import clean_text_for_ingestion

    current_chunks = {(row["document_id"], row["chunk_number"]): row for row in chunks}
    accepted = set()
    for row in documents:
        owner = projection_owner(row["indexed_key"])
        if owner is not None and owner != row["id"]:
            continue
        for hit in hits:
            if hit.get("doc_id") != row["indexed_key"] or hit.get("instance") != row["instance"].lower():
                continue
            if owner is None:
                peers = [item for item in documents if item["indexed_key"] == row["indexed_key"] and item["instance"] == row["instance"]]
                if len(peers) > 1 and hit.get("workflow_id") != row["temporal_workflow_id"]:
                    continue
            chunk = hit.get("chunk_num")
            current = current_chunks.get((row["id"], chunk))
            if row["passages"] is not None:
                if not any(p.get("chunk_num") == chunk and p.get("text") == hit.get("text") for p in row["passages"]):
                    continue
                if row["frozen_workflow_id"] == row["current_workflow_id"] and current and current["is_excluded"]:
                    continue
            elif not current or current["is_excluded"] or clean_text_for_ingestion(current["text"]) != hit.get("text"):
                continue
            accepted.add(id(hit))
    return [hit for hit in hits if id(hit) in accepted]


def visible_release_hits(hits: list[dict], *, network_id: str | None = None) -> list[dict]:
    """Hide staged, superseded and withdrawn revision points from retrieval."""
    revision_ids = {
        hit.get("revision_id") for hit in hits if common._is_uuid(str(hit.get("revision_id")))
    }
    if not revision_ids and not any(hit.get("release_network_id") for hit in hits):
        return [] if network_id else hits
    with connection.get_connection() as conn:
        rows = conn.execute(
            "SELECT n.revision_id::text, n.network_id FROM network_releases n "
            "JOIN documents d ON d.id = n.document_id "
            "WHERE n.revision_id = ANY(%s::uuid[]) AND n.withdrawn_at IS NULL AND NOT d.is_disabled "
            "AND d.metadata->>'network_visible' IS DISTINCT FROM 'false'",
            (list(revision_ids),),
        ).fetchall()
    active = {(row["revision_id"], row["network_id"]) for row in rows}
    return [
        hit
        for hit in hits
        if (network_id is None or hit.get("release_network_id") == network_id)
        and ((network_id is None and not hit.get("revision_id") and not hit.get("release_network_id"))
             or (hit.get("revision_id"), hit.get("release_network_id")) in active)
    ]


def inactive_network_revision_ids(network_id: str, collection: str) -> list[str]:
    """Read staged and retired revisions to exclude before vector ranking."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            "SELECT r.id::text FROM network_release_staging s "
            "JOIN document_revisions r ON r.id = s.revision_id JOIN documents d ON d.id = r.document_id "
            "LEFT JOIN network_releases n ON n.document_id = d.id AND n.network_id = s.network_id "
            "WHERE s.collection = %s AND s.network_id = %s "
            "AND (n.revision_id IS DISTINCT FROM r.id OR n.withdrawn_at IS NOT NULL "
            "OR d.is_disabled OR d.metadata->>'network_visible' = 'false') "
            "LIMIT 1001",
            (collection, network_id),
        ).fetchall()
    if len(rows) > 1000:
        raise ValueError("This release scope exceeds the complete pre-ranking filter limit")
    return [row["id"] for row in rows]


# =============================================================================
# Vector index state (documents.metadata.index_status)
# =============================================================================


def _index_entry_to_legacy(workflow_id: str, key: str, entry: dict) -> dict:
    details = entry.get("details") or {}
    return {
        "workflow_id": workflow_id,
        "index_name": entry.get("index_name") or key,
        "doc_id": entry.get("doc_id"),
        "chunk_count_indexed": entry.get("chunk_count_indexed") or 0,
        "last_indexed_at": entry.get("last_indexed_at"),
        "last_verified_at": entry.get("last_verified_at"),
        "schema_version": entry.get("schema_version"),
        "status": entry.get("status") or "unknown",
        "details_json": json.dumps(details) if details else None,
    }


def upsert_document_index_status(
    workflow_id: str,
    index_name: str,
    doc_id: Optional[str] = None,
    chunk_count_indexed: Optional[int] = None,
    last_indexed_at: Optional[str] = None,
    last_verified_at: Optional[str] = None,
    schema_version: Optional[str] = None,
    status: str = "unknown",
    details: Optional[dict] = None,
    publication: bool | None = None,
) -> dict:
    """Merge one index's state into ``documents.metadata.index_status``.

    Each collection owns an independent entry. ``None`` arguments leave the
    stored value alone, except ``status`` which always replaces it.
    """
    key = index_key_for(index_name)
    with connection.get_connection() as conn:
        doc_uuid = document_reads._require_document_uuid(conn, workflow_id)
        row = conn.execute(
            "SELECT metadata->'index_status'->%s::text AS entry, "
            "metadata->'superseded_doc_ids' AS superseded FROM documents WHERE id = %s::uuid FOR UPDATE",
            (key, doc_uuid),
        ).fetchone()
        existing = dict(row["entry"] or {})
        old_key = existing.get("doc_id")
        if old_key and doc_id and old_key != doc_id:
            superseded = {k: list(v) for k, v in (row["superseded"] or {}).items()}
            if old_key not in superseded.get(key, []):
                superseded.setdefault(key, []).append(old_key)
            conn.execute(
                "UPDATE documents SET metadata = metadata || "
                "jsonb_build_object('superseded_doc_ids', %s::jsonb) WHERE id = %s::uuid",
                (Jsonb(superseded), doc_uuid),
            )
        incoming = {
            "index_name": index_name,
            "doc_id": doc_id,
            "chunk_count_indexed": chunk_count_indexed,
            "last_indexed_at": last_indexed_at,
            "last_verified_at": last_verified_at,
            "schema_version": schema_version,
            "details": details or None,
            "publication": publication,
        }
        merged = {
            **existing,
            **{k: v for k, v in incoming.items() if v is not None},
            "status": status,
        }
        validated = IndexState.model_validate(merged).model_dump()
        conn.execute(
            "UPDATE documents SET metadata = jsonb_set("
            "metadata || jsonb_build_object('index_status', COALESCE(metadata->'index_status', '{}'::jsonb)), "
            "ARRAY['index_status', %s::text], %s::jsonb) "
            "WHERE id = %s::uuid",
            (key, Jsonb(validated), doc_uuid),
        )
        root = jobs.root_workflow_id(conn, doc_uuid) or workflow_id
    return _index_entry_to_legacy(root, key, validated)


def list_document_index_status(workflow_id: str) -> list[dict]:
    doc = document_reads.get_document(workflow_id)
    if not doc:
        return []
    entries = [
        _index_entry_to_legacy(doc["workflow_id"], key, entry)
        for key, entry in doc["index_status"].items()
    ]
    entries.sort(
        key=lambda e: (e["last_verified_at"] or "", e["last_indexed_at"] or ""), reverse=True
    )
    return entries


def get_document_index_status(workflow_id: str, index_name: str) -> Optional[dict]:
    for entry in list_document_index_status(workflow_id):
        if entry["index_name"] == index_name:
            return entry
    return None


def list_index_summaries(
    include_disabled: bool = False, instances: list[str] | None = None
) -> list[dict]:
    clauses, params = document_reads._document_filters(include_disabled, instances)
    where = f"AND {' AND '.join(clauses)}" if clauses else ""
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(e.value->>'index_name', e.key) AS index_name,
                COUNT(*) AS documents,
                SUM(COALESCE((e.value->>'chunk_count_indexed')::int, 0)) AS indexed_chunks,
                SUM(CASE WHEN COALESCE((d.metadata->>'reindex_required')::boolean, FALSE)
                         THEN 1 ELSE 0 END) AS stale_documents,
                MAX(e.value->>'last_indexed_at') AS last_indexed_at,
                array_agg(DISTINCT COALESCE(e.value->>'status', 'unknown')) AS statuses
            FROM documents d
            JOIN instances g ON g.id = d.provider_id
            CROSS JOIN LATERAL jsonb_each(COALESCE(d.metadata->'index_status', '{{}}'::jsonb)) e
            WHERE TRUE {where}
            GROUP BY COALESCE(e.value->>'index_name', e.key)
            ORDER BY index_name
            """,
            params,
        ).fetchall()
    result = []
    for row in rows:
        item = common._clean_row(row)
        item["indexed_chunks"] = int(item["indexed_chunks"] or 0)
        item["stale_documents"] = int(item["stale_documents"] or 0)
        item["statuses"] = sorted(s for s in row["statuses"] if s)
        result.append(item)
    return result


def find_document_by_doc_identifier(identifier: str) -> Optional[dict]:
    """Resolve a vector-store/chat doc identifier to a document.

    Matches the document UUID, a Temporal workflow id, the fingerprint, the
    filename, an index ``doc_id``, or the legacy ``md5(fingerprint)`` doc id.
    """
    if not identifier:
        return None
    with connection.get_connection() as conn:
        return document_reads._fetch_document(
            conn, document_reads._resolve_document_uuid(conn, identifier)
        )


def resolve_chunk_provenance(
    *,
    doc_id: Optional[str] = None,
    chunk_num: Optional[int] = None,
) -> Optional[dict]:
    """Resolve workflow + chunk metadata for provenance links."""
    if not doc_id or chunk_num is None:
        return None

    doc = find_document_by_doc_identifier(doc_id)
    if not doc:
        return None

    workflow_id = doc["workflow_id"]
    chunk = chunk_queries.get_chunk(workflow_id, int(chunk_num))
    if not chunk:
        return None

    text = chunk.get("edited_text") or chunk.get("original_text") or ""
    excerpt = text[:320] + ("..." if len(text) > 320 else "")
    page_start = int(chunk.get("page_start") or 1)
    page_end = int(chunk.get("page_end") or page_start)
    doc_name = doc.get("name") or doc.get("filename") or workflow_id

    return {
        "workflow_id": workflow_id,
        "doc_id": doc.get("document_id"),
        "filename": workflow_id,
        "doc_name": doc_name,
        "chunk_num": int(chunk_num),
        "page_start": page_start,
        "page_end": page_end,
        "page_range": f"{page_start}-{page_end}" if page_end != page_start else str(page_start),
        "section": "",
        "excerpt": excerpt,
    }
