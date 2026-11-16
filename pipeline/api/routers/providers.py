"""Provider identity and scoped Keycloak membership management."""

import logging
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, StringConstraints

from pipeline import db
from pipeline.intake import provider_sources, scheduled_intake
from pipeline.auth import keycloak_admin
from pipeline.auth.config import load_auth_config
from pipeline.auth.deps import CurrentUser, RequireAdmin
from pipeline.auth.groups import ROLE_STATE_ADMIN
from pipeline.auth.models import AuthUser
from pipeline.auth.permissions import Permission
from pipeline.auth.tenancy import allowed_instances, default_instance, portal_instance

router = APIRouter(prefix="/providers", tags=["providers"])
logger = logging.getLogger(__name__)

ProviderCode = Annotated[str, Path(pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?$")]
UserId = Annotated[str, Path(pattern=r"^[A-Za-z0-9:_-]{1,200}$")]
ProviderRole = Literal["state_admin", "state_approver", "state_contributor", "state_view"]
Email = Annotated[str, StringConstraints(strip_whitespace=True, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")]


class OnboardProvider(BaseModel):
    admin_email: Email


class ChangeProviderRole(BaseModel):
    role: ProviderRole


class GrantProviderRole(ChangeProviderRole):
    email: Email


def _require_live_management(user: AuthUser) -> None:
    if user.token_disabled_mode or not load_auth_config().verify_live_groups:
        raise HTTPException(503, "Provider management requires live Keycloak authorization")


def _require_provider_manager(user: AuthUser, provider: str) -> None:
    _require_live_management(user)
    if not user.is_superadmin and user.role_for_instance(provider) != ROLE_STATE_ADMIN:
        raise HTTPException(403, "Provider administrator access is required")


def _set_member(provider: str, user_id: str, role: ProviderRole | None, actor: AuthUser) -> dict:
    with db.get_connection() as conn:
        # Serialize scoped updates to a user's group set across API replicas.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (
            f"provider-membership:{provider}:{user_id}",
        ))
        result = keycloak_admin.set_provider_membership(
            provider, user_id, role, allow_admin_changes=actor.is_superadmin,
        )
    logger.info("provider_membership_changed provider=%s member=%s role=%s actor=%s", provider, user_id, role, actor.user_id)
    return result


@router.get("")
def list_providers(user: CurrentUser):
    _require_live_management(user)
    scope = None if user.is_superadmin else sorted(
        code for code, role in user.state_roles.items() if role == ROLE_STATE_ADMIN
    )
    if scope == []:
        raise HTTPException(403, "Provider administrator access is required")
    return {"providers": db.list_provider_instances(scope)}


@router.get("/readable")
def list_readable_providers(user: CurrentUser):
    """List provider choices for document and index reads."""
    if not user.has_permission(Permission.SEARCH):
        raise HTTPException(403, "Search permission is required")
    scope = allowed_instances(user)
    if scope is not None:
        return {"providers": sorted(scope)}
    return {"providers": [row["provider"] for row in db.list_provider_instances(None)]}


@router.put("/{provider}")
def onboard_provider(provider: ProviderCode, body: OnboardProvider, user: RequireAdmin):
    """Create a provider identity and appoint an existing Keycloak account as administrator."""
    _require_provider_manager(user, provider)
    if provider in {default_instance(), portal_instance()}:
        raise HTTPException(409, "This provider code is reserved")
    if "_" in provider:
        raise HTTPException(422, "Use letters, numbers and hyphens for a new provider code")
    admin_id = keycloak_admin.find_verified_user_id(body.admin_email)
    group = keycloak_admin.create_provider_group(provider)
    instance_id = db.upsert_instance(group["path"], keycloak_group_id=group["id"])
    _set_member(provider, admin_id, ROLE_STATE_ADMIN, user)
    return {"id": instance_id, "provider": provider, "group": group["path"], "admin_user_id": admin_id}


@router.get("/{provider}/members")
def list_members(provider: ProviderCode, user: CurrentUser):
    _require_provider_manager(user, provider)
    return {"provider": provider, "members": keycloak_admin.list_provider_memberships(provider)}


@router.get("/{provider}/scheduled-sources")
def list_scheduled_sources(provider: ProviderCode, user: CurrentUser):
    _require_provider_manager(user, provider)
    try:
        sources = scheduled_intake.provider_pull_status(provider)
    except (OSError, RecursionError, ValueError) as exc:
        raise HTTPException(503, "Scheduled intake configuration is unavailable") from exc
    return {"provider": provider, "checked_at": datetime.now(timezone.utc), "sources": sources}


@router.post("/{provider}/members")
def grant_member(provider: ProviderCode, body: GrantProviderRole, user: CurrentUser):
    _require_provider_manager(user, provider)
    if body.role == ROLE_STATE_ADMIN and not user.is_superadmin:
        raise HTTPException(403, "Only a platform administrator can grant provider administrator access")
    user_id = keycloak_admin.find_verified_user_id(body.email)
    return _set_member(provider, user_id, body.role, user)


@router.put("/{provider}/members/{user_id}")
def change_member(provider: ProviderCode, user_id: UserId, body: ChangeProviderRole, user: CurrentUser):
    _require_provider_manager(user, provider)
    return _set_member(provider, user_id, body.role, user)


@router.delete("/{provider}/members/{user_id}")
def revoke_member(provider: ProviderCode, user_id: UserId, user: CurrentUser):
    _require_provider_manager(user, provider)
    return _set_member(provider, user_id, None, user)


@router.get("/{provider}/source-settings")
def get_source_settings(provider: ProviderCode, user: CurrentUser):
    _require_provider_manager(user, provider)
    try:
        return provider_sources.public_settings(provider_sources.source_settings(provider))
    except (OSError, RecursionError, ValueError) as exc:
        raise HTTPException(503, "Source settings are unavailable") from exc


@router.put("/{provider}/source-settings")
def update_source_settings(provider: ProviderCode, body: provider_sources.SourceSettingsUpdate, user: CurrentUser):
    _require_provider_manager(user, provider)
    try:
        return provider_sources.save_settings(provider, body, user.user_id)
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, "Provider not found") from exc
    except (OSError, RecursionError, ValueError) as exc:
        raise HTTPException(422, "Invalid source settings. Check source hosts, schedules, templates and access policies.") from exc
