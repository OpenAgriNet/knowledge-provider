"""Record and query the audit trail."""

import json
from contextlib import nullcontext
from typing import Optional

from psycopg.types.json import Jsonb

from . import catalog, common, connection, document_reads, jobs, providers

# =============================================================================
# Audit Log Functions
# =============================================================================

_AUDIT_ENTITY_TYPES = {"document", "page", "chunk", "setting", "catalog"}


def log_audit(
    workflow_id: str,
    document_id: str,
    action_type: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    field_name: Optional[str] = None,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
    metadata: Optional[dict] = None,
    actor_user_id: Optional[str] = None,
    actor_username: Optional[str] = None,
    actor_email: Optional[str] = None,
    actor_roles: Optional[str] = None,
    *,
    _conn=None,
) -> str:
    """
    Log an audit entry.

    Args:
        workflow_id: The Temporal workflow ID (or document identifier). Ids that
            start with ``__`` mark system-level entries and are not stored.
        document_id: Unused; kept for call-site compatibility. The document is
            resolved from ``workflow_id``.
        action_type: Type of action (stage_change, page_edit, chunk_edit, approval, reset)
        entity_type: Type of entity (document, page, chunk, setting, catalog)
        entity_id: Page number or chunk number the action applied to
        field_name: Name of the field that changed
        old_value: Previous value (as string or JSON)
        new_value: New value (as string or JSON)
        metadata: Additional context as dict
        actor_*: Identity of the human/system that performed the action

    Returns:
        The UUID of the created audit log entry
    """
    entity = entity_type if entity_type in _AUDIT_ENTITY_TYPES else None
    system_entry = (workflow_id or "").startswith("__")
    roles = common._csv_roles(actor_roles)
    with connection.get_connection() if _conn is None else nullcontext(_conn) as conn:
        doc_uuid = (
            None if system_entry else document_reads._resolve_document_uuid(conn, workflow_id)
        )
        instance_uuid = None
        stored_workflow_id = None if system_entry else workflow_id
        if doc_uuid:
            doc_row = conn.execute(
                "SELECT provider_id FROM documents WHERE id = %s::uuid",
                (doc_uuid,),
            ).fetchone()
            instance_uuid = str(doc_row["provider_id"])
            if common._is_uuid(workflow_id):
                stored_workflow_id = jobs.root_workflow_id(conn, doc_uuid) or workflow_id
        if stored_workflow_id is None and entity not in ("setting", "catalog"):
            stored_workflow_id = workflow_id
        actor_uuid = (
            providers.upsert_user(actor_user_id, actor_username, actor_email, roles, conn=conn)
            if actor_user_id
            else None
        )
        row = conn.execute(
            """
            INSERT INTO audit_logs (document_id, workflow_id, instance_id, action_type, entity_type,
                                    entity_number, field_name, old_value, new_value, metadata,
                                    actor_user_id, actor)
            VALUES (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s::uuid, %s)
            RETURNING id
            """,
            (
                doc_uuid,
                stored_workflow_id,
                instance_uuid,
                action_type,
                entity,
                entity_id,
                field_name,
                old_value,
                new_value,
                Jsonb(metadata or {}),
                actor_uuid,
                Jsonb(
                    {
                        "username": actor_username or None,
                        "email": actor_email or None,
                        "roles": roles,
                    }
                )
                if (actor_user_id or actor_username or actor_email or roles)
                else Jsonb({}),
            ),
        ).fetchone()
        return str(row["id"])


# Keycloak attaches these to every account regardless of what the user can
# actually do here. Rendering them made the actor column a wall of noise
# ("...manage-account,manage-account-links,offline_access,uma_authorization...")
# that buried the one role a reviewer cares about.
_NOISE_ROLES = {
    "manage-account",
    "manage-account-links",
    "offline_access",
    "uma_authorization",
    "view-profile",
    "account",
}


# Most-privileged first, so a multi-role account is labelled by what it can do.
_ROLE_PRECEDENCE = (
    "superadmin",
    "super_admin",
    "master_admin",
    "admin",
    "content_curator",
    "reviewer",
    "contributor",
    "viewer",
)


def meaningful_roles(raw: Optional[str]) -> list[str]:
    """Application roles from a stored role list, Keycloak built-ins removed."""
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    kept = [
        p
        for p in parts
        if p.lower() not in _NOISE_ROLES and not p.lower().startswith("default-roles")
    ]
    ranked = [r for r in _ROLE_PRECEDENCE if r in {k.lower() for k in kept}]
    if ranked:
        # Preserve original casing of the first matching role.
        first = next(k for k in kept if k.lower() == ranked[0])
        return [first]
    return kept[:1]


def _normalize_audit_row(row) -> dict:
    entry = dict(row)
    email = (entry.get("actor_email") or "").strip()
    username = (entry.get("actor_username") or "").strip()
    roles = meaningful_roles(entry.get("actor_roles"))
    role_label = roles[0] if roles else ""

    who = email or username
    if who and role_label:
        entry["actor"] = f"{who} ({role_label})"
    elif who:
        entry["actor"] = who
    else:
        entry["actor"] = "system"

    # Split out so the UI can render identity and role separately rather than
    # parsing the combined string back apart.
    entry["actor_display"] = who or "System"
    entry["actor_role"] = role_label
    entry["is_system"] = not who
    return entry


_AUDIT_SELECT = """
    SELECT a.id, a.workflow_id, a.action_type, a.entity_type, a.entity_number, a.field_name,
           a.old_value, a.new_value, a.metadata, a.created_at, a.actor, ag.name AS audit_instance,
           au.keycloak_user_id AS actor_keycloak_id,
           d.source->>'fingerprint' AS doc_fingerprint,
           d.source->>'original_filename' AS filename,
           d.name AS document_name,
           dg.name AS doc_instance,
           uu.username AS uploaded_by_username, uu.email AS uploaded_by_email
    FROM audit_logs a
    LEFT JOIN documents d ON d.id = a.document_id
    LEFT JOIN instances ag ON ag.id = a.instance_id
    LEFT JOIN instances dg ON dg.id = d.provider_id
    LEFT JOIN users au ON au.id = a.actor_user_id
    LEFT JOIN users uu ON uu.id = NULLIF(d.metadata->>'uploaded_by_user_id', '')::uuid
"""


def _audit_row_to_legacy(row: dict) -> dict:
    actor = row["actor"] or {}
    metadata = row["metadata"] or {}
    entity_type = row["entity_type"]
    if entity_type == "setting":
        workflow_id, document_id = "__system__", "__settings__"
    elif entity_type == "catalog" and not row["workflow_id"]:
        workflow_id, document_id = "__catalog__", "__catalog__"
    else:
        workflow_id = row["workflow_id"]
        document_id = row["doc_fingerprint"] or workflow_id
    roles = actor.get("roles") or []
    entry = {
        "id": row["id"],
        "workflow_id": workflow_id,
        "document_id": document_id,
        "action_type": row["action_type"],
        "entity_type": entity_type,
        "entity_id": row["entity_number"],
        "field_name": row["field_name"],
        "old_value": row["old_value"],
        "new_value": row["new_value"],
        "metadata": json.dumps(metadata) if metadata else None,
        "timestamp": row["created_at"],
        "actor_user_id": row["actor_keycloak_id"],
        "actor_username": actor.get("username"),
        "actor_email": actor.get("email"),
        "actor_roles": ",".join(roles) if roles else None,
        "filename": row["filename"],
        "name": row["document_name"],
        "instance": row["doc_instance"] or row["audit_instance"],
        "uploaded_by_username": row["uploaded_by_username"],
        "uploaded_by_email": row["uploaded_by_email"],
    }
    return _normalize_audit_row({k: common._clean(v) for k, v in entry.items()})


def _audit_scope(workflow_id: str, conn) -> tuple[str, list]:
    doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
    if doc_uuid:
        root = jobs.root_workflow_id(conn, doc_uuid) or workflow_id
        return "(a.document_id = %s::uuid OR a.workflow_id = %s)", [doc_uuid, root]
    return "a.workflow_id = %s", [workflow_id]


def get_audit_logs(
    workflow_id: str, action_type: Optional[str] = None, limit: int = 100, offset: int = 0
) -> list[dict]:
    """
    Get audit logs for a document.

    Args:
        workflow_id: The Temporal workflow ID (or document UUID)
        action_type: Optional filter by action type
        limit: Maximum number of entries to return
        offset: Offset for pagination

    Returns:
        List of audit log entries as dicts
    """
    with connection.get_connection() as conn:
        where, params = _audit_scope(workflow_id, conn)
        if action_type:
            where += " AND a.action_type = %s"
            params.append(action_type)
        rows = conn.execute(
            f"{_AUDIT_SELECT} WHERE {where} ORDER BY a.created_at DESC, a.id LIMIT %s OFFSET %s",
            (*params, limit, offset),
        ).fetchall()
    return [_audit_row_to_legacy(r) for r in rows]


def get_audit_log_count(workflow_id: str, action_type: Optional[str] = None) -> int:
    """Get count of audit logs for a document."""
    with connection.get_connection() as conn:
        where, params = _audit_scope(workflow_id, conn)
        if action_type:
            where += " AND a.action_type = %s"
            params.append(action_type)
        row = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM audit_logs a WHERE {where}", params
        ).fetchone()
    return int(row["cnt"]) if row else 0


def _global_audit_filters(
    action_type: Optional[str], instances: Optional[list[str]]
) -> tuple[list[str], list]:
    where_parts: list[str] = ["a.action_type <> %s"]
    params: list = [catalog.CATALOG_VERSION_ACTION]
    if action_type:
        where_parts.append("a.action_type = %s")
        params.append(action_type)
    if instances is not None:
        where_parts.append("lower(COALESCE(ag.name, dg.name, %s)) = ANY(%s)")
        params.extend([common._default_instance(), common._normalize_instances(instances)])
    return where_parts, params


def get_all_audit_logs(
    action_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    instances: Optional[list[str]] = None,
) -> list[dict]:
    """
    Get all audit logs across all documents.

    Args:
        action_type: Optional filter by action type
        limit: Maximum number of entries to return
        offset: Offset for pagination
        instances: Tenant scope (None = all, [] = none)

    Returns:
        List of audit log entries as dicts, with document filename included
    """
    if instances is not None and not common._normalize_instances(instances):
        return []
    where_parts, params = _global_audit_filters(action_type, instances)
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"{_AUDIT_SELECT} WHERE {' AND '.join(where_parts)} "
            "ORDER BY a.created_at DESC, a.id LIMIT %s OFFSET %s",
            (*params, limit, offset),
        ).fetchall()
    return [_audit_row_to_legacy(r) for r in rows]


def get_all_audit_log_count(
    action_type: Optional[str] = None,
    instances: Optional[list[str]] = None,
) -> int:
    """Get total count of all audit logs (optionally tenant-scoped)."""
    if instances is not None and not common._normalize_instances(instances):
        return 0
    where_parts, params = _global_audit_filters(action_type, instances)
    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM audit_logs a "
            "LEFT JOIN documents d ON d.id = a.document_id "
            "LEFT JOIN instances ag ON ag.id = a.instance_id "
            "LEFT JOIN instances dg ON dg.id = d.provider_id "
            f"WHERE {' AND '.join(where_parts)}",
            params,
        ).fetchone()
    return int(row["cnt"]) if row else 0
