"""Maintain the catalog projection and its change version."""

from typing import Optional

from psycopg.types.json import Jsonb

from pipeline.network.network_constants import catalog_type_for_kind, document_kind_schemas

from . import common, connection, document_reads


def _catalog_seed_rows() -> tuple[list[str], list[str]]:
    """Parallel ``(types, kinds)`` lists for every supported catalog type."""
    mapping = document_kind_schemas()
    kinds = list(mapping)
    return [mapping[k] for k in kinds], kinds


def ensure_instance_catalogs(instance_id: str, conn=None) -> None:
    """Create the instance's missing catalogs, one per supported type; existing rows are untouched."""
    if conn is None:
        with connection.get_connection() as own:
            return ensure_instance_catalogs(instance_id, conn=own)
    types, kinds = _catalog_seed_rows()
    conn.execute(
        """
        INSERT INTO catalogs (instance_id, kind, type, name)
        SELECT g.id, t.kind, t.type, upper(g.name) || ' ' || t.kind || ' catalog'
        FROM instances g, unnest(%s::text[], %s::text[]) AS t(type, kind)
        WHERE g.id = %s::uuid
        ON CONFLICT (instance_id, kind) DO UPDATE SET type = EXCLUDED.type
        WHERE catalogs.type IS DISTINCT FROM EXCLUDED.type
        """,
        (types, kinds, instance_id),
    )


def sync_catalogs_for_all_instances(conn=None) -> int:
    """Add every missing ``(instance, type)`` catalog; returns how many rows were created.

    Idempotent. Run at startup so a type added to the kind map reaches instances
    that already exist.
    """
    if conn is None:
        with connection.get_connection() as own:
            return sync_catalogs_for_all_instances(conn=own)
    types, kinds = _catalog_seed_rows()
    cur = conn.execute(
        """
        INSERT INTO catalogs (instance_id, kind, type, name)
        SELECT g.id, t.kind, t.type, upper(g.name) || ' ' || t.kind || ' catalog'
        FROM instances g, unnest(%s::text[], %s::text[]) AS t(type, kind)
        ON CONFLICT (instance_id, kind) DO UPDATE SET type = EXCLUDED.type
        WHERE catalogs.type IS DISTINCT FROM EXCLUDED.type
        """,
        (types, kinds),
    )
    return cur.rowcount


def get_or_create_catalog(instance_id: str, kind: str, conn=None) -> dict:
    """The ``(instance, kind)`` catalog row; created when missing (normally seeded with the instance)."""
    if conn is None:
        with connection.get_connection() as own:
            return get_or_create_catalog(instance_id, kind, conn=own)
    catalog_type = catalog_type_for_kind(kind)
    row = conn.execute(
        """
        INSERT INTO catalogs (instance_id, kind, type, name)
        SELECT g.id, %s, %s, upper(g.name) || ' ' || %s || ' catalog' FROM instances g WHERE g.id = %s::uuid
        ON CONFLICT (instance_id, kind) DO UPDATE SET type = EXCLUDED.type
        RETURNING id, instance_id, type, name, metadata
        """,
        (kind, catalog_type, kind, instance_id),
    ).fetchone()
    return common._clean_row(row)


# =============================================================================
# Catalog
# =============================================================================

CATALOG_VERSION_ACTION = "catalog_version_bump"


def get_catalog_meta() -> dict:
    """Catalog version: the number of version bumps recorded in ``audit_logs``."""
    with connection.get_connection() as conn:
        row = conn.execute(
            """
            SELECT count(*) AS version, max(created_at) AS updated_at,
                   (array_agg(metadata->>'reason' ORDER BY created_at DESC))[1] AS notes
            FROM audit_logs
            WHERE entity_type = 'catalog' AND action_type = %s
            """,
            (CATALOG_VERSION_ACTION,),
        ).fetchone()
    return {
        "id": 1,
        "version": int(row["version"] or 0),
        "updated_at": common._clean(row["updated_at"]),
        "notes": row["notes"],
    }


def bump_catalog_version(reason: str = "", *, conn=None) -> int:
    """Monotonic catalog version bump. Returns new version."""
    if conn is None:
        with connection.get_connection() as owned:
            return bump_catalog_version(reason, conn=owned)
    conn.execute(
        "INSERT INTO audit_logs (action_type, entity_type, metadata) VALUES (%s, 'catalog', %s)",
        (CATALOG_VERSION_ACTION, Jsonb({"reason": (reason or "")[:500]})),
    )
    row = conn.execute(
        "SELECT count(*) AS version FROM audit_logs WHERE entity_type = 'catalog' AND action_type = %s",
        (CATALOG_VERSION_ACTION,),
    ).fetchone()
    return int(row["version"])


def list_document_codes(kind: str) -> set[str]:
    """Every document code in catalogs of one knowledge kind."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            "SELECT code FROM documents WHERE kind = %s",
            (kind,),
        ).fetchall()
    return {r["code"] for r in rows}


def _catalog_row(row: dict) -> dict:
    cleaned = common._clean_row(row)
    cleaned["type"] = catalog_type_for_kind(row["kind"])
    cleaned.pop("instance_name", None)
    cleaned["aliases"] = list(row["aliases"] or [])
    cleaned["instances"] = [(row["instance_name"] or "").lower()]
    cleaned["workflow_ids"] = [row["workflow_id"]] if row["workflow_id"] else []
    cleaned["source"] = "pipeline"
    return cleaned


def list_catalog_entries(status: Optional[str] = None, kind: str = "scheme", *, network_id: str | None = None) -> list[dict]:
    """Rows of the ``catalog_status`` view (one per document) for one knowledge kind."""
    catalog_type_for_kind(kind)
    with connection.get_connection() as conn:
        if network_id:
            if status not in (None, "live"):
                return []
            rows = conn.execute(
                """
                SELECT d.id AS document_id, c.id AS catalog_id, d.provider_id AS instance_id,
                       i.name AS instance_name, c.type, d.kind,
                       r.snapshot->'metadata'->>'code' AS code,
                       r.snapshot->'metadata'->>'name' AS name,
                       COALESCE(r.snapshot->'metadata'->'aliases', '[]'::jsonb) AS aliases,
                       'live'::text AS status, w.temporal_workflow_id AS workflow_id,
                       jsonb_array_length(COALESCE(r.snapshot->'passages', '[]'::jsonb)) AS chunk_count,
                       true AS network_visible, n.released_at AS promoted_at,
                       n.network_id, n.revision_id::text
                FROM network_releases n
                JOIN document_revisions r ON r.id = n.revision_id AND r.document_id = n.document_id
                JOIN documents d ON d.id = n.document_id
                JOIN instances i ON i.id = d.provider_id
                JOIN workflows w ON w.id = r.workflow_id
                LEFT JOIN catalogs c ON c.instance_id = d.provider_id AND c.kind = d.kind
                WHERE n.network_id = %s AND d.kind = %s AND n.withdrawn_at IS NULL AND NOT d.is_disabled
                  AND d.metadata->>'network_visible' IS DISTINCT FROM 'false'
                  AND d.metadata->>'catalog_visible' IS DISTINCT FROM 'false'
                ORDER BY code
                """,
                (network_id, kind),
            ).fetchall()
        elif status:
            rows = conn.execute(
                "SELECT * FROM catalog_status WHERE kind = %s AND status = %s ORDER BY code",
                (kind, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM catalog_status WHERE kind = %s ORDER BY code", (kind,)
            ).fetchall()
    return [_catalog_row(r) for r in rows]


def get_catalog_entry(code: str, kind: str = "scheme") -> Optional[dict]:
    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM catalog_status WHERE kind = %s AND code = %s LIMIT 1",
            (kind, code),
        ).fetchone()
    return _catalog_row(row) if row else None


def list_used_scheme_codes(
    exclude_workflow_id: Optional[str] = None, kind: str = "scheme"
) -> set[str]:
    """Every document code already taken in a kind - the collision domain for auto-generated codes.

    The excluded document's own code is not counted, so re-deriving a
    document's code does not collide with itself.
    """
    with connection.get_connection() as conn:
        doc_uuid = (
            document_reads._resolve_document_uuid(conn, exclude_workflow_id)
            if exclude_workflow_id
            else None
        )
        rows = conn.execute(
            """
            SELECT code FROM documents
            WHERE kind = %s AND (%s::uuid IS NULL OR id <> %s::uuid)
            """,
            (kind, doc_uuid, doc_uuid),
        ).fetchall()
    return {r["code"] for r in rows}


def list_scheme_catalog_entries_for_workflow(workflow_id: str) -> list[dict]:
    """Catalog rows whose documents still include this workflow."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            "SELECT code, workflow_id FROM catalog_status WHERE kind = %s AND workflow_id = %s",
            ("scheme", workflow_id),
        ).fetchall()
    return [{"code": r["code"], "workflow_ids": [r["workflow_id"]]} for r in rows]
