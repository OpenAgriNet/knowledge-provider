import asyncio

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from pipeline.auth.deps import RequireManageUsers
from pipeline.auth.keycloak_admin import (
    list_access_options,
    list_realm_users,
    provision_user,
    set_user_access,
)

router = APIRouter()


class ProvisionUserRequest(BaseModel):
    """Fields a super admin must fill to create / update access."""

    email: str = Field(..., description="Google SSO email (required)")
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    access_type: str = Field(
        ...,
        description="super_admin | global_viewer | state",
    )
    state: str = Field("", description="State code e.g. MH (required for state access)")
    role: str = Field(
        "",
        description="state_admin | state_view (required for state access; aliases: admin, view, contributor, reviewer)",
    )
    enabled: bool = True


class SetUserAccessRequest(BaseModel):
    """Change an existing user's role in the selected scope."""

    access_type: str = Field(..., description="super_admin | global_viewer | state")
    state: str = Field("", description="State/centre code e.g. MH (required when access_type=state)")
    role: str = Field(
        "",
        description=(
            "state_admin | state_approver | state_contributor | state_view "
            "(required when access_type=state)"
        ),
    )


@router.get("/admin/access-options")
async def admin_access_options(user: RequireManageUsers):
    """Form options + required fields for the Users admin UI."""
    return list_access_options()


@router.get("/admin/users")
async def admin_list_users(
    user: RequireManageUsers,
    search: str = "",
    max_results: int = Query(100, ge=1, le=200),
):
    """List Keycloak users with groups/access labels (no delete)."""
    return await asyncio.to_thread(list_realm_users, search=search, max_results=max_results)


@router.post("/admin/users")
async def admin_provision_user(data: ProvisionUserRequest, user: RequireManageUsers):
    """Create or update a Keycloak user and assign group/role. Returns share text."""
    return await asyncio.to_thread(
        provision_user,
        email=data.email,
        first_name=data.first_name,
        last_name=data.last_name,
        username=data.username or None,
        access_type=data.access_type,
        state=data.state or None,
        role=data.role or None,
        enabled=data.enabled,
    )


@router.put("/admin/users/{user_id}/access")
async def admin_set_user_access(
    user_id: str, data: SetUserAccessRequest, user: RequireManageUsers
):
    """Change an existing user's role in one scope (super admin only)."""
    return await asyncio.to_thread(
        set_user_access,
        user_id=user_id,
        access_type=data.access_type,
        state=data.state or None,
        role=data.role or None,
    )
