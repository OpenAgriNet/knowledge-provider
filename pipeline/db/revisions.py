"""Read and freeze revision snapshots and activate network releases."""

import hashlib
import json
from contextlib import nullcontext

from psycopg.types.json import Jsonb

from pipeline.metadata.schema_packs import validate_schema_selection

from . import chunks as chunk_queries
from . import common, connection, document_reads
from . import pages as page_queries


def _check_revision_job(conn, document_id: str, workflow_id: str, network_id: str | None) -> str:
    row = conn.execute(
        "SELECT d.is_disabled, d.metadata->>'network_visible' AS network_visible, "
        "w.id, w.temporal_workflow_id, w.config->>'release_network_id' AS release_network_id "
        "FROM documents d JOIN document_current_workflow w ON w.document_id = d.id "
        "WHERE d.id = %s::uuid FOR UPDATE OF d",
        (document_id,),
    ).fetchone()
    if (
        not row
        or row["is_disabled"]
        or (network_id is not None and row["network_visible"] == "false")
        or row["temporal_workflow_id"] != workflow_id
        or row["release_network_id"] != network_id
    ):
        raise ValueError("The release is no longer authorized for this document workflow")
    return str(row["id"])


def load_revision_material(workflow_id: str) -> tuple[dict, list[dict], list[dict]]:
    """Read the document, pages and chunks from one consistent database snapshot."""
    with connection.get_connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        document_id = document_reads._require_document_uuid(conn, workflow_id)
        doc = document_reads._fetch_document(conn, document_id)
        pages = conn.execute(
            page_queries._PAGE_SELECT + " WHERE p.document_id = %s::uuid ORDER BY p.page_number",
            (document_id,),
        ).fetchall()
        chunks = conn.execute(
            chunk_queries._CHUNK_SELECT + " WHERE c.document_id = %s::uuid ORDER BY c.chunk_number",
            (document_id,),
        ).fetchall()
    return (
        doc,
        [page_queries._page_from_row(row, workflow_id) for row in pages],
        [chunk_queries._chunk_from_row(row, workflow_id) for row in chunks],
    )


def _insert_revision_snapshot(
    conn, document_id: str, job_id: str, revision_id: str, snapshot: dict
) -> dict:
    existing = conn.execute(
        "SELECT id::text, snapshot FROM document_revisions WHERE workflow_id = %s::uuid",
        (job_id,),
    ).fetchone()
    if existing:
        return existing
    validate_schema_selection(conn, (snapshot.get("metadata") or {}).get("knowledge_metadata"))
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return conn.execute(
        """
        INSERT INTO document_revisions (id, document_id, workflow_id, content_sha256, snapshot)
        VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s)
        RETURNING id::text, snapshot
        """,
        (revision_id, document_id, job_id, digest, Jsonb(snapshot)),
    ).fetchone()


def save_draft_revision_snapshot(
    document_id: str, workflow_id: str, revision_id: str, snapshot: dict
) -> dict:
    """Freeze the current workflow's reviewed content independently of publication."""
    with connection.get_connection() as conn:
        job_id = _check_revision_job(conn, document_id, workflow_id, None)
        return _insert_revision_snapshot(conn, document_id, job_id, revision_id, snapshot)


def save_revision_snapshot(
    document_id: str,
    workflow_id: str,
    network_id: str,
    revision_id: str,
    snapshot: dict,
    *,
    collection: str | None = None,
    provider_id: str | None = None,
) -> dict:
    """Freeze content and stage its network projection together. The first snapshot wins."""
    with connection.get_connection() as conn:
        job_id = _check_revision_job(conn, document_id, workflow_id, network_id)
        current = _locked_network_release(conn, document_id, network_id)
        revision = _insert_revision_snapshot(conn, document_id, job_id, revision_id, snapshot)
        conn.execute(
            "INSERT INTO network_release_staging (revision_id, network_id, collection, provider_id, expected_generation) "
            "VALUES (%s::uuid, %s, %s, %s, %s::uuid) ON CONFLICT (revision_id, network_id) DO NOTHING",
            (
                revision["id"],
                network_id,
                collection,
                provider_id,
                current["generation"] if current else None,
            ),
        )
        stage = conn.execute(
            "SELECT collection, provider_id FROM network_release_staging WHERE revision_id = %s::uuid AND network_id = %s",
            (revision["id"], network_id),
        ).fetchone()
        return {**revision, "stage": stage}


def _locked_network_release(conn, document_id: str, network_id: str) -> dict | None:
    return conn.execute(
        "SELECT generation::text, transaction_id::text, revision_id::text FROM network_releases "
        "WHERE document_id = %s::uuid AND network_id = %s FOR UPDATE",
        (document_id, network_id),
    ).fetchone()


def activate_network_release(
    document_id: str, workflow_id: str, network_id: str, transaction_id: str
) -> None:
    """Switch the network pointer only after its adapter accepts the release."""
    with connection.get_connection() as conn:
        job_id = _check_revision_job(conn, document_id, workflow_id, network_id)
        revision = conn.execute(
            "SELECT r.id::text, s.generation_captured AS has_generation, "
            "s.expected_generation::text AS expected_generation "
            "FROM document_revisions r JOIN network_release_staging s ON s.revision_id = r.id "
            "WHERE r.workflow_id = %s::uuid AND s.network_id = %s",
            (job_id, network_id),
        ).fetchone()
        if not revision:
            raise ValueError("The workflow has no revision to release")
        current = _locked_network_release(conn, document_id, network_id)
        if current and current["revision_id"] == revision["id"]:
            return
        generation = current["generation"] if current else None
        if not revision["has_generation"] or revision["expected_generation"] != generation:
            raise ValueError("The network release changed after this revision was staged")
        conn.execute(
            "INSERT INTO network_releases (document_id, network_id, revision_id, transaction_id) "
            "VALUES (%s::uuid, %s, %s::uuid, %s::uuid) "
            "ON CONFLICT (document_id, network_id) DO UPDATE SET revision_id = excluded.revision_id, "
            "transaction_id = excluded.transaction_id, released_at = now(), withdrawn_at = NULL, generation = gen_random_uuid()",
            (document_id, network_id, revision["id"], transaction_id),
        )


def list_document_revisions(document_id: str) -> dict:
    """Return revision history and current destinations without loading full snapshots."""
    with connection.get_connection() as conn:
        revisions = conn.execute(
            "SELECT id::text, content_sha256, created_at, snapshot->'metadata' AS metadata, "
            "snapshot->'source'->>'filename' AS filename FROM document_revisions "
            "WHERE document_id = %s::uuid ORDER BY created_at DESC, id DESC LIMIT 100",
            (document_id,),
        ).fetchall()
        releases = conn.execute(
            "SELECT network_id, revision_id::text, generation::text, released_at, withdrawn_at FROM network_releases "
            "WHERE document_id = %s::uuid ORDER BY network_id",
            (document_id,),
        ).fetchall()
    return {
        "revisions": [common._clean_row(row) for row in revisions],
        "releases": [common._clean_row(row) for row in releases],
    }


def save_legacy_index_revisions(candidates: list[tuple[str, str, dict]]) -> None:
    """Record historical search evidence without changing current workflow state."""
    with connection.get_connection() as conn:
        for doc_id, revision_id, snapshot in candidates:
            document = conn.execute(
                "SELECT created_at, is_disabled FROM documents WHERE id = %s::uuid FOR UPDATE",
                (doc_id,),
            ).fetchone()
            if not document or document["is_disabled"]:
                raise ValueError("Migrated document became unavailable")
            saved = conn.execute(
                "SELECT snapshot FROM document_revisions WHERE id = %s::uuid",
                (revision_id,),
            ).fetchone()
            if saved:
                if saved["snapshot"] != snapshot:
                    raise ValueError(
                        "Existing legacy search revision differs from the supplied evidence"
                    )
                continue
            job_id = conn.execute(
                "INSERT INTO workflows (document_id, workflow_type, temporal_workflow_id, status, current_stage, "
                "config, started_at, completed_at) VALUES (%s::uuid, 'legacy_index_import', %s, "
                "'completed', 'completed', %s, %s::timestamptz - interval '1 second', "
                "%s::timestamptz - interval '1 second') RETURNING id",
                (
                    doc_id,
                    f"legacy-index-{revision_id}",
                    Jsonb(snapshot["legacy_search_import"]),
                    document["created_at"],
                    document["created_at"],
                ),
            ).fetchone()["id"]
            _insert_revision_snapshot(conn, doc_id, str(job_id), revision_id, snapshot)


def get_latest_completed_index_revision(document_id: str) -> dict | None:
    with connection.get_connection() as conn:
        return conn.execute(
            "SELECT r.id::text, r.snapshot FROM document_revisions r "
            "JOIN workflows w ON w.id = r.workflow_id "
            "WHERE r.document_id = %s::uuid AND w.status = 'completed' "
            "AND w.config->>'release_network_id' IS NULL "
            "ORDER BY (w.workflow_type = 'legacy_index_import'), r.created_at DESC, r.id DESC LIMIT 1",
            (document_id,),
        ).fetchone()


def get_revision_snapshot(document_id: str, revision_id: str) -> dict | None:
    if not common._is_uuid(revision_id):
        return None
    with connection.get_connection() as conn:
        return conn.execute(
            "SELECT id::text, content_sha256, snapshot FROM document_revisions "
            "WHERE document_id = %s::uuid AND id = %s::uuid",
            (document_id, revision_id),
        ).fetchone()


def withdraw_network_release(
    document_id: str, network_id: str, revision_id: str, *, _conn=None
) -> bool:
    if not common._is_uuid(revision_id):
        return False
    with connection.get_connection() if _conn is None else nullcontext(_conn) as conn:
        result = conn.execute(
            "UPDATE network_releases SET withdrawn_at = COALESCE(withdrawn_at, now()), "
            "generation = CASE WHEN withdrawn_at IS NULL THEN gen_random_uuid() ELSE generation END "
            "WHERE document_id = %s::uuid AND network_id = %s AND revision_id = %s::uuid",
            (document_id, network_id, revision_id),
        )
    return result.rowcount > 0


def stage_saved_network_revision(
    document_id: str,
    network_id: str,
    revision_id: str,
    transaction_id: str,
    expected_generation: str | None,
    collection: str,
    provider_id: str | None,
) -> bool:
    """Register the projection before indexing so it remains hidden until selection."""
    with connection.get_connection() as conn:
        document = conn.execute(
            """
            SELECT d.is_disabled, d.metadata->>'network_visible' AS network_visible
            FROM documents d JOIN document_revisions r ON r.document_id = d.id
            WHERE d.id = %s::uuid AND r.id = %s::uuid FOR UPDATE OF d
            """,
            (document_id, revision_id),
        ).fetchone()
        if not document or document["is_disabled"] or document["network_visible"] == "false":
            raise ValueError("The revision is unavailable for release")
        current = _locked_network_release(conn, document_id, network_id)
        if current and current["transaction_id"] == transaction_id:
            return False
        if (current["generation"] if current else None) != expected_generation:
            raise ValueError("The network release changed. Reload before selecting a revision")
        stage = conn.execute(
            """
            INSERT INTO network_release_staging
                (revision_id, network_id, collection, provider_id, generation_captured)
            VALUES (%s::uuid, %s, %s, %s, FALSE)
            ON CONFLICT (revision_id, network_id) DO UPDATE
            SET collection = COALESCE(network_release_staging.collection, EXCLUDED.collection),
                provider_id = COALESCE(network_release_staging.provider_id, EXCLUDED.provider_id)
            WHERE (network_release_staging.collection IS NULL OR network_release_staging.collection = EXCLUDED.collection)
              AND (network_release_staging.provider_id IS NULL OR network_release_staging.provider_id = EXCLUDED.provider_id)
            RETURNING revision_id
            """,
            (revision_id, network_id, collection, provider_id),
        ).fetchone()
        if not stage:
            raise ValueError("The network projection no longer matches the staged release")
    return True


def select_network_revision(
    document_id: str,
    network_id: str,
    revision_id: str,
    transaction_id: str,
    expected_generation: str | None,
) -> None:
    with connection.get_connection() as conn:
        document = conn.execute(
            "SELECT is_disabled, metadata->>'network_visible' AS network_visible FROM documents "
            "WHERE id = %s::uuid FOR UPDATE",
            (document_id,),
        ).fetchone()
        if not document or document["is_disabled"] or document["network_visible"] == "false":
            raise ValueError("The document is unavailable for release")
        current = _locked_network_release(conn, document_id, network_id)
        if current and current["transaction_id"] == transaction_id:
            return
        if (current["generation"] if current else None) != expected_generation:
            raise ValueError("The network release changed. Reload before selecting a revision")
        conn.execute(
            "INSERT INTO network_releases (document_id, network_id, revision_id, transaction_id) "
            "VALUES (%s::uuid, %s, %s::uuid, %s::uuid) "
            "ON CONFLICT (document_id, network_id) DO UPDATE SET revision_id = excluded.revision_id, "
            "transaction_id = excluded.transaction_id, released_at = now(), withdrawn_at = NULL, generation = gen_random_uuid()",
            (document_id, network_id, revision_id, transaction_id),
        )
