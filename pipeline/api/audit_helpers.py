"""Actor normalization and audit serialization for API mutations."""

import json

from . import api as _api


def _parse_roles_field(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except Exception:
            pass
    return [part.strip() for part in text.split(",") if part.strip()]


def _actor_from_user(user) -> dict:
    """Normalize AuthUser into serializable actor identity fields."""
    if user is None:
        return {
            "user_id": "",
            "username": "",
            "email": "",
            "roles": [],
            "roles_csv": "",
        }
    roles = [str(r).strip() for r in (getattr(user, "roles", None) or []) if str(r).strip()]
    return {
        "user_id": (getattr(user, "user_id", None) or "").strip(),
        "username": (getattr(user, "username", None) or "").strip(),
        "email": (getattr(user, "email", None) or "").strip(),
        "roles": roles,
        "roles_csv": ",".join(roles),
    }


def _log_audit(
    workflow_id: str,
    action_type: str,
    entity_type: str = None,
    entity_id: int = None,
    field_name: str = None,
    old_value=None,
    new_value=None,
    metadata: dict = None,
    user=None,
    *,
    _conn=None,
):
    """
    Helper to log audit entries with JSON serialization and optional actor identity.

    Args:
        workflow_id: The Temporal workflow ID
        action_type: Type of action (page_edit, chunk_edit, approval, etc.)
        entity_type: Type of entity (page, chunk)
        entity_id: Entity identifier (page_number, chunk_number)
        field_name: Name of the field changed
        old_value: Previous value (will be JSON serialized if not string)
        new_value: New value (will be JSON serialized if not string)
        metadata: Additional context as dict
        user: Optional AuthUser whose email/roles are stored on the audit row
    """
    # Get document_id from Postgres
    doc = _api.db.get_document(workflow_id) if _conn is None else None
    document_id = doc["document_id"] if doc else workflow_id

    # Serialize values to JSON if needed
    old_str = json.dumps(old_value) if old_value is not None and not isinstance(old_value, str) else old_value
    new_str = json.dumps(new_value) if new_value is not None and not isinstance(new_value, str) else new_value

    actor = _api._actor_from_user(user)
    meta = dict(metadata or {})
    if actor.get("email") or actor.get("username") or actor.get("roles"):
        meta.setdefault("actor", {
            "user_id": actor.get("user_id"),
            "username": actor.get("username"),
            "email": actor.get("email"),
            "roles": actor.get("roles"),
        })

    _api.db.log_audit(
        workflow_id=workflow_id,
        document_id=document_id,
        action_type=action_type,
        entity_type=entity_type,
        entity_id=entity_id,
        field_name=field_name,
        old_value=old_str,
        new_value=new_str,
        metadata=meta or None,
        actor_user_id=actor.get("user_id") or None,
        actor_username=actor.get("username") or None,
        actor_email=actor.get("email") or None,
        actor_roles=actor.get("roles_csv") or None,
        **({"_conn": _conn} if _conn is not None else {}),
    )
