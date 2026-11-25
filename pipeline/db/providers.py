"""Store instances and attribution records for Keycloak identities."""

from typing import Any, Optional

import psycopg

from pipeline.auth.groups import provider_group_root
from pipeline.auth.tenancy import configured_portal_instance, portal_instance

from . import catalog, common, connection

# =============================================================================
# Instances
# =============================================================================


def _tenant_path(instance: str) -> str:
    """Path for an instance, including a configured legacy portal projection."""
    return (
        "/global"
        if instance == configured_portal_instance()
        else f"/{provider_group_root()}/{instance.upper()}"
    )


def _tenant_code_from_path(path: str) -> str:
    return portal_instance() if path == "/global" else path.rstrip("/").rsplit("/", 1)[-1].lower()


def upsert_instance(path: str, keycloak_group_id: Optional[str] = None, conn=None) -> str:
    """Create or refresh the ``instances`` row for a Keycloak group path; returns its UUID.

    Rows made by the SQLite move have no Keycloak id yet; the first sync that
    sees the path fills it in. The instance's catalogs (one per supported type) are
    ensured in the same transaction, so an instance never exists without them.
    """
    if conn is None:
        with connection.get_connection() as own:
            return upsert_instance(path, keycloak_group_id, conn=own)
    row = conn.execute(
        """
        INSERT INTO instances (keycloak_group_id, name, path) VALUES (%s, %s, %s)
        ON CONFLICT (path) DO UPDATE SET
            keycloak_group_id = COALESCE(EXCLUDED.keycloak_group_id, instances.keycloak_group_id)
        RETURNING id
        """,
        (keycloak_group_id, _tenant_code_from_path(path), path),
    ).fetchone()
    instance_id = str(row["id"])
    catalog.ensure_instance_catalogs(instance_id, conn=conn)
    return instance_id


def list_provider_instances(allowed_codes: list[str] | None) -> list[dict]:
    """List Keycloak-backed provider identities visible to the caller."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, name AS provider, path AS "group", display_name
            FROM instances
            WHERE keycloak_group_id IS NOT NULL AND split_part(path, '/', 2) = %s
              AND (%s::text[] IS NULL OR name = ANY(%s::text[]))
            ORDER BY name
            """,
            (provider_group_root(), allowed_codes, allowed_codes),
        ).fetchall()
    return [common._clean_row(row) for row in rows]


def set_instance_display_name(provider: str, display_name: str | None) -> dict | None:
    """Update the presentation label of a registered provider."""
    with connection.get_connection() as conn:
        row = conn.execute(
            """
            UPDATE instances SET display_name = %s, updated_at = now()
            WHERE name = %s AND path = %s AND keycloak_group_id IS NOT NULL
            RETURNING id, name AS provider, path AS "group", display_name
            """,
            (display_name, provider, _tenant_path(provider)),
        ).fetchone()
    return common._clean_row(row)


def get_or_create_instance(instance: Optional[str], conn=None) -> str:
    """UUID of the instance for a tenant code (``bv``, ``mh``, ...), created when missing."""
    code = (instance or common._default_instance()).strip().lower() or common._default_instance()
    return upsert_instance(_tenant_path(code), conn=conn)


# =============================================================================
# Users
# =============================================================================


def upsert_user(
    keycloak_user_id: str,
    username: Optional[str] = None,
    email: Optional[str] = None,
    roles: Any = None,
    conn=None,
    tenants: Optional[list[str]] = None,
) -> Optional[str]:
    """Create or refresh the ``users`` row for a Keycloak subject; returns its UUID.

    ``tenants`` are the tenant codes in the user's token. A user has one
    ``instance_id``, so the first code (sorted) wins and a warning is logged when
    there are several. Authorization still reads the token, not this column.
    """
    keycloak_user_id = (keycloak_user_id or "").strip()
    if not keycloak_user_id:
        return None
    if conn is None:
        with connection.get_connection() as own:
            return upsert_user(keycloak_user_id, username, email, roles, conn=own, tenants=tenants)

    role_list = common._csv_roles(roles)
    tenant_codes = sorted({(t or "").strip().lower() for t in (tenants or []) if (t or "").strip()})
    if len(tenant_codes) > 1:
        connection.logger.warning(
            "user %s has several tenants %s; users.instance_id uses %s",
            keycloak_user_id,
            tenant_codes,
            tenant_codes[0],
        )
    instance_uuid = get_or_create_instance(tenant_codes[0], conn=conn) if tenant_codes else None
    email = (email or "").strip() or None
    username = (username or "").strip() or None
    for candidate_email in [email, None] if email else [None]:
        try:
            with conn.transaction():
                row = conn.execute(
                    """
                    INSERT INTO users (keycloak_user_id, username, email, roles, instance_id)
                    VALUES (%s, %s, %s, %s, %s::uuid)
                    ON CONFLICT (keycloak_user_id) DO UPDATE SET
                        username = COALESCE(EXCLUDED.username, users.username),
                        email = COALESCE(EXCLUDED.email, users.email),
                        roles = CASE WHEN cardinality(EXCLUDED.roles) > 0
                                     THEN EXCLUDED.roles ELSE users.roles END,
                        instance_id = COALESCE(EXCLUDED.instance_id, users.instance_id),
                        last_seen_at = now()
                    RETURNING id
                    """,
                    (keycloak_user_id, username, candidate_email, role_list, instance_uuid),
                ).fetchone()
            return str(row["id"])
        except psycopg.errors.UniqueViolation:
            continue
    return None


def get_user_by_keycloak_id(keycloak_user_id: str) -> Optional[dict]:
    with connection.get_connection() as conn:
        return common._clean_row(
            conn.execute(
                "SELECT * FROM users WHERE keycloak_user_id = %s", (keycloak_user_id,)
            ).fetchone()
        )
