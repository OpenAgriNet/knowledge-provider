"""Maintain the catalog projection and its change version."""

from typing import Optional

from psycopg.types.json import Jsonb

from pipeline.network.network_constants import KIND_TO_CATALOG_TYPE, catalog_type_for_kind

from . import common, connection, document_reads


def _catalog_seed_rows() -> tuple[list[str], list[str]]:
    """Parallel ``(types, kinds)`` lists for every supported catalog type."""
    kinds = list(KIND_TO_CATALOG_TYPE)
    return [KIND_TO_CATALOG_TYPE[k] for k in kinds], kinds


def ensure_instance_catalogs(instance_id: str, conn=None) -> None:
    """Create the instance's missing catalogs, one per supported type; existing rows are untouched."""
    if conn is None:
        with connection.get_connection() as own:
            return ensure_instance_catalogs(instance_id, conn=own)
    types, kinds = _catalog_seed_rows()
    conn.execute(
        """
        INSERT INTO catalogs (instance_id, type, name)
        SELECT g.id, t.type, upper(g.name) || ' ' || t.kind || ' catalog'
        FROM instances g, unnest(%s::text[], %s::text[]) AS t(type, kind)
        WHERE g.id = %s::uuid
        ON CONFLICT (instance_id, type) DO NOTHING
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
        INSERT INTO catalogs (instance_id, type, name)
        SELECT g.id, t.type, upper(g.name) || ' ' || t.kind || ' catalog'
        FROM instances g, unnest(%s::text[], %s::text[]) AS t(type, kind)
        ON CONFLICT (instance_id, type) DO NOTHING
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
        INSERT INTO catalogs (instance_id, type, name)
        SELECT g.id, %s, upper(g.name) || ' ' || %s || ' catalog' FROM instances g WHERE g.id = %s::uuid
        ON CONFLICT (instance_id, type) DO UPDATE SET type = EXCLUDED.type
        RETURNING id, instance_id, type, name, metadata
        """,
        (catalog_type, kind, instance_id),
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


def bump_catalog_version(reason: str = "") -> int:
    """Monotonic catalog version bump. Returns new version."""
    with connection.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_logs (action_type, entity_type, metadata)
            VALUES (%s, 'catalog', %s)
            """,
            (CATALOG_VERSION_ACTION, Jsonb({"reason": (reason or "")[:500]})),
        )
        row = conn.execute(
            "SELECT count(*) AS version FROM audit_logs "
            "WHERE entity_type = 'catalog' AND action_type = %s",
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
    cleaned.pop("instance_name", None)
    cleaned["aliases"] = list(row["aliases"] or [])
    cleaned["instances"] = [(row["instance_name"] or "").lower()]
    cleaned["workflow_ids"] = [row["workflow_id"]] if row["workflow_id"] else []
    cleaned["source"] = "pipeline"
    return cleaned


def list_catalog_entries(status: Optional[str] = None, kind: str = "scheme") -> list[dict]:
    """Rows of the ``catalog_status`` view (one per document) for one knowledge kind."""
    catalog_type = catalog_type_for_kind(kind)
    with connection.get_connection() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM catalog_status WHERE type = %s AND status = %s ORDER BY code",
                (catalog_type, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM catalog_status WHERE type = %s ORDER BY code", (catalog_type,)
            ).fetchall()
    return [_catalog_row(r) for r in rows]


def get_catalog_entry(code: str, kind: str = "scheme") -> Optional[dict]:
    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM catalog_status WHERE type = %s AND code = %s LIMIT 1",
            (catalog_type_for_kind(kind), code),
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
            "SELECT code, workflow_id FROM catalog_status WHERE type = %s AND workflow_id = %s",
            (catalog_type_for_kind("scheme"), workflow_id),
        ).fetchall()
    return [{"code": r["code"], "workflow_ids": [r["workflow_id"]]} for r in rows]
