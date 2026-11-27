"""Parse Keycloak group paths into tenants (states) and roles.

Expected Keycloak group layout (full paths in the JWT ``groups`` claim):

- ``/global/super-admin``               → super_admin (all states, full access)
- ``/global/global-viewer``             → global_viewer (all providers, read-only)
- ``/states/{CODE}/admin``              → state_admin
- ``/states/{CODE}/approver``           → state_approver
- ``/states/{CODE}/contributor``        → state_contributor
- ``/states/{CODE}/view``               → state_view

``{CODE}`` is any tenant code — a state (``MH``) or the centre. Centre is
modelled as just another tenant, so it needs no separate tree.

A user may hold different roles in different states.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# Canonical product roles (JWT / Keycloak group leaves + realm roles).
ROLE_SUPER_ADMIN = "super_admin"
ROLE_GLOBAL_VIEWER = "global_viewer"
ROLE_STATE_ADMIN = "state_admin"
ROLE_STATE_APPROVER = "state_approver"
ROLE_STATE_CONTRIBUTOR = "state_contributor"
ROLE_STATE_VIEW = "state_view"

# Roles that apply to a single tenant, in descending order of power.
STATE_ROLES = (
    ROLE_STATE_ADMIN,
    ROLE_STATE_APPROVER,
    ROLE_STATE_CONTRIBUTOR,
    ROLE_STATE_VIEW,
)

GLOBAL_VIEWER_ROLE_ALIASES = frozenset({
    ROLE_GLOBAL_VIEWER, "global-viewer", "bh_viewer", "bh-viewer", "bh_view", "bh-view",
})
GLOBAL_VIEWER_GROUP_PATH = "/global/global-viewer"

# Roles that apply across every tenant.
GLOBAL_ROLES = (ROLE_SUPER_ADMIN, ROLE_GLOBAL_VIEWER)

CANONICAL_ROLES = frozenset(GLOBAL_ROLES + STATE_ROLES)

# Higher number wins when a user is in multiple groups for the same state.
_ROLE_RANK: dict[str, int] = {
    ROLE_SUPER_ADMIN: 100,
    ROLE_STATE_ADMIN: 60,
    ROLE_STATE_APPROVER: 40,
    ROLE_STATE_CONTRIBUTOR: 20,
    ROLE_STATE_VIEW: 10,
    ROLE_GLOBAL_VIEWER: 5,
}

# Leaf / realm-role spellings → canonical role.
#
# NOTE: ``contributor`` deliberately maps to the *weakest* upload role now.
# Before the six-role model it aliased to state_admin, so never reintroduce
# that mapping — it would silently re-grant edit/approve/delete.
_ROLE_ALIASES: dict[str, str] = {
    # Global platform roles
    "super_admin": ROLE_SUPER_ADMIN,
    "super-admin": ROLE_SUPER_ADMIN,
    "superadmin": ROLE_SUPER_ADMIN,
    **dict.fromkeys(GLOBAL_VIEWER_ROLE_ALIASES, ROLE_GLOBAL_VIEWER),
    # Per state / centre
    "state_admin": ROLE_STATE_ADMIN,
    "state-admin": ROLE_STATE_ADMIN,
    "admin": ROLE_STATE_ADMIN,
    "state_approver": ROLE_STATE_APPROVER,
    "state-approver": ROLE_STATE_APPROVER,
    "approver": ROLE_STATE_APPROVER,
    "state_contributor": ROLE_STATE_CONTRIBUTOR,
    "state-contributor": ROLE_STATE_CONTRIBUTOR,
    "contributor": ROLE_STATE_CONTRIBUTOR,
    "state_view": ROLE_STATE_VIEW,
    "state-view": ROLE_STATE_VIEW,
    "view": ROLE_STATE_VIEW,
}

_STATE_GROUP_RE = re.compile(
    r"^/(?P<root>[A-Za-z0-9_-]+)/(?P<state>[A-Za-z0-9_-]+)/(?P<role>[A-Za-z0-9_-]+)/?$",
    re.IGNORECASE,
)


def provider_group_root() -> str:
    root = os.environ.get("KEYCLOAK_PROVIDER_GROUP_ROOT", "providers").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", root) or root.casefold() == "global":
        raise ValueError("KEYCLOAK_PROVIDER_GROUP_ROOT must name one provider group root")
    return root
_GLOBAL_SUPER_RE = re.compile(
    r"^/global/(?P<role>super[_-]?admin|superadmin)/?$",
    re.IGNORECASE,
)
_GLOBAL_VIEWER_RE = re.compile(
    r"^/global/(?P<role>global[_-]viewer|bh[_-]?viewer|bh[_-]?view)/?$",
    re.IGNORECASE,
)


def normalize_role(value: str | None) -> str | None:
    """Normalize a role leaf or realm role name to a canonical role, if known."""
    if not value:
        return None
    key = value.strip().lower().replace(" ", "_")
    if not key:
        return None
    if key in _ROLE_ALIASES:
        return _ROLE_ALIASES[key]
    # super_admin-style after hyphen→underscore
    underscored = key.replace("-", "_")
    if underscored in _ROLE_ALIASES:
        return _ROLE_ALIASES[underscored]
    return None


def normalize_state_code(value: str | None) -> str:
    """State/tenant codes are stored lowercase (mh, up, bh)."""
    return (value or "").strip().lower()


@dataclass
class GroupAccess:
    """Access derived from Keycloak ``groups`` claim paths."""

    is_super_admin: bool = False
    is_global_viewer: bool = False
    # instance (state) → highest role in that state
    state_roles: dict[str, str] = field(default_factory=dict)
    # flat list of group paths as received
    groups: list[str] = field(default_factory=list)
    # union of canonical roles (includes super_admin when global)
    roles: list[str] = field(default_factory=list)

    @property
    def instances(self) -> list[str]:
        """State codes the user may access (empty when super-admin = all)."""
        return sorted(self.state_roles.keys())


def _prefer_role(current: str | None, candidate: str) -> str:
    if current is None:
        return candidate
    return candidate if _ROLE_RANK.get(candidate, 0) > _ROLE_RANK.get(current, 0) else current


def parse_group_paths(groups: Iterable[str] | None) -> GroupAccess:
    """Parse full Keycloak group paths into super-admin flag + per-state roles."""
    access = GroupAccess()
    if not groups:
        return access
    group_root = provider_group_root().casefold()

    paths: list[str] = []
    for raw in groups:
        if not isinstance(raw, str):
            continue
        path = raw.strip()
        if not path:
            continue
        # Normalize: ensure leading slash, collapse trailing slash (except root)
        if not path.startswith("/"):
            path = "/" + path
        path = path.rstrip("/") or "/"
        paths.append(path)

        if _GLOBAL_SUPER_RE.match(path):
            access.is_super_admin = True
            continue

        if _GLOBAL_VIEWER_RE.match(path):
            access.is_global_viewer = True
            continue

        state_match = _STATE_GROUP_RE.match(path)
        if not state_match or state_match.group("root").casefold() != group_root:
            # A membership in the provider group itself grants read access.
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0].casefold() == group_root:
                state = normalize_state_code(parts[1])
                if state:
                    access.state_roles.setdefault(state, ROLE_STATE_VIEW)
            continue

        state = normalize_state_code(state_match.group("state"))
        role = normalize_role(state_match.group("role"))
        if not state or role not in STATE_ROLES:
            continue
        access.state_roles[state] = _prefer_role(access.state_roles.get(state), role)

    access.groups = sorted(set(paths))
    role_set: set[str] = set()
    if access.is_super_admin:
        role_set.add(ROLE_SUPER_ADMIN)
    if access.is_global_viewer:
        role_set.add(ROLE_GLOBAL_VIEWER)
    role_set.update(access.state_roles.values())
    access.roles = sorted(role_set, key=lambda r: (-_ROLE_RANK.get(r, 0), r))
    return access


def extract_groups_claim(claims: dict[str, Any]) -> list[str]:
    """Read multivalued ``groups`` (or ``group``) claim from a JWT payload."""
    for key in ("groups", "group"):
        raw = claims.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
            if parts:
                return parts
        if isinstance(raw, (list, tuple)):
            return [str(item).strip() for item in raw if str(item).strip()]
    return []


def role_for_instance(access: GroupAccess, instance: str | None) -> str | None:
    """Return the user's role for a state instance, or SUPER_ADMIN when global.

    A ``global_viewer`` reads every provider, so any
    instance resolves to STATE_VIEW unless a stronger state role is held.
    """
    if access.is_super_admin:
        return ROLE_SUPER_ADMIN
    if not instance:
        return None
    role = access.state_roles.get(normalize_state_code(instance))
    if role:
        return role
    return ROLE_STATE_VIEW if access.is_global_viewer else None


# Canonical role → Keycloak group leaf name.
_ROLE_LEAF: dict[str, str] = {
    ROLE_SUPER_ADMIN: "super-admin",
    ROLE_GLOBAL_VIEWER: "global-viewer",
    ROLE_STATE_ADMIN: "admin",
    ROLE_STATE_APPROVER: "approver",
    ROLE_STATE_CONTRIBUTOR: "contributor",
    ROLE_STATE_VIEW: "view",
}


def group_leaf_for_role(role: str | None) -> str:
    """Preferred Keycloak group leaf name for a product role."""
    canon = normalize_role(role) or (role or "").strip().lower()
    return _ROLE_LEAF.get(canon, canon or "view")
