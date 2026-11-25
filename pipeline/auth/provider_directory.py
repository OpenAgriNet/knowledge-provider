"""Read the instance directory used by access, intake and document browsing."""

from pipeline import db

from . import keycloak_admin
from .groups import provider_group_root
from .tenancy import configured_portal_instance


def list_visible_providers(allowed_codes: list[str] | None = None) -> list[dict]:
    """Use live Keycloak groups and attach existing PostgreSQL registration IDs."""
    registered = db.list_provider_instances(allowed_codes)
    groups = keycloak_admin.list_provider_groups()
    if groups is None:
        return registered

    registrations = {row["provider"]: row for row in registered}
    root = provider_group_root()
    allowed = set(allowed_codes) if allowed_codes is not None else None
    providers = []
    for group in groups:
        name = group["name"]
        code = name.lower()
        if allowed is None or code in allowed:
            registration = registrations.get(code, {})
            providers.append({
                "id": registration.get("id"), "provider": code, "group": f"/{root}/{name}",
                "display_name": registration.get("display_name"),
            })
    return sorted(providers, key=lambda row: row["provider"])


def list_document_instance_codes() -> set[str]:
    """Include the portal namespace when listing places that can own documents."""
    codes = {row["provider"] for row in list_visible_providers()}
    portal = configured_portal_instance()
    if portal:
        codes.add(portal)
    return codes
