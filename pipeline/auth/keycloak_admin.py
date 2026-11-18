"""Keycloak Admin API helper for super-admin user provisioning (Plan 2).

Creates/updates users and assigns groups/roles in the same realm used for SSO.
Requires admin credentials via env (see ENV.md). Does not store access in the app DB.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from .groups import (
    GLOBAL_VIEWER_GROUP_PATH,
    GLOBAL_VIEWER_ROLE_ALIASES,
    ROLE_GLOBAL_VIEWER,
    ROLE_STATE_ADMIN,
    ROLE_STATE_APPROVER,
    ROLE_STATE_CONTRIBUTOR,
    ROLE_STATE_VIEW,
    ROLE_SUPER_ADMIN,
    STATE_ROLES,
    group_leaf_for_role,
    normalize_role,
    parse_group_paths,
    provider_group_root,
)
from .tenancy import portal_instance

# Realm-role descriptions written when a role is auto-created.
STATE_ROLE_DESCRIPTIONS: dict[str, str] = {
    ROLE_STATE_ADMIN: "State admin — upload, edit, review & approve, pipeline, delete own",
    ROLE_STATE_APPROVER: "State approver — upload, edit, review & approve, pipeline",
    ROLE_STATE_CONTRIBUTOR: "State contributor — upload and run pipeline only",
    ROLE_STATE_VIEW: "State view — read-only access in assigned state",
}

# Global access types accepted by ``ensure_access_group``.
_SUPER_ADMIN_ACCESS_TYPES = ("super_admin", "super-admin", "portal")

@dataclass
class KeycloakAdminConfig:
    base_url: str
    realm: str
    client_id: str
    client_secret: str = field(repr=False)
    token_realm: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.realm and self.client_id and self.client_secret)


def load_keycloak_admin_config() -> KeycloakAdminConfig:
    """Load admin settings; derive base/realm from KEYCLOAK_ISSUER when possible."""
    issuer = (os.environ.get("KEYCLOAK_ISSUER") or "").strip().rstrip("/")
    base = (os.environ.get("KEYCLOAK_ADMIN_BASE_URL") or "").strip().rstrip("/")
    realm = (os.environ.get("KEYCLOAK_ADMIN_REALM") or "").strip()

    if issuer and "/realms/" in issuer:
        # https://host/auth/realms/bharat-vistaar
        head, _, realm_part = issuer.partition("/realms/")
        if not base:
            base = head
        if not realm:
            realm = realm_part.split("/")[0]

    return KeycloakAdminConfig(
        base_url=base,
        realm=realm,
        token_realm=(os.environ.get("KEYCLOAK_ADMIN_TOKEN_REALM") or realm).strip(),
        client_id=(os.environ.get("KEYCLOAK_ADMIN_CLIENT_ID") or "").strip(),
        client_secret=os.environ.get("KEYCLOAK_ADMIN_CLIENT_SECRET") or "",
    )


def _req(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: Any = None,
    form: dict | None = None,
) -> tuple[int, Any]:
    headers: dict[str, str] = {}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read()
            if not raw:
                return response.status, None
            return response.status, json.loads(raw.decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except Exception:
            detail = raw
        raise HTTPException(exc.code if 400 <= exc.code < 600 else 502, detail) from exc
    except urllib.error.URLError as exc:
        raise HTTPException(502, f"Keycloak admin unreachable: {exc}") from exc


def _admin_token(cfg: KeycloakAdminConfig) -> str:
    token_realm = cfg.token_realm or cfg.realm
    form = {"grant_type": "client_credentials", "client_id": cfg.client_id, "client_secret": cfg.client_secret}
    status, body = _req(
        "POST",
        f"{cfg.base_url}/realms/{token_realm}/protocol/openid-connect/token",
        form=form,
    )
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        raise HTTPException(502, "Unable to obtain Keycloak admin token")
    return str(body["access_token"])


def _admin_root(cfg: KeycloakAdminConfig) -> str:
    return f"{cfg.base_url}/admin/realms/{cfg.realm}"


def require_admin_config() -> KeycloakAdminConfig:
    cfg = load_keycloak_admin_config()
    if not cfg.configured:
        raise HTTPException(
            503,
            "Keycloak admin is not configured. Configure a realm service account.",
        )
    return cfg


def _user_admin_url(admin: str, user_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9:_-]{1,200}", user_id):
        raise HTTPException(400, "Invalid Keycloak user ID")
    return f"{admin}/users/{urllib.parse.quote(user_id, safe='')}"


def _list_keycloak(admin_url: str, token: str) -> list[dict]:
    page_size = 100
    result = []
    while True:
        _, page = _req("GET", f"{admin_url}?first={len(result)}&max={page_size}", token=token)
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            raise HTTPException(502, "Invalid Keycloak list response")
        result.extend(page)
        if len(page) < page_size:
            return result


def current_user_group_paths(user_id: str) -> list[str]:
    """Read current memberships, including removal since the JWT was issued."""
    cfg = require_admin_config()
    try:
        user_url = _user_admin_url(_admin_root(cfg), user_id)
        token = _admin_token(cfg)
        _, identity = _req("GET", user_url, token=token)
        if not isinstance(identity, dict) or identity.get("enabled") is not True:
            raise HTTPException(401, "Keycloak user is inactive")
        groups = _list_keycloak(f"{user_url}/groups", token)
    except HTTPException as exc:
        if exc.status_code in (400, 401, 404):
            raise HTTPException(401, "Keycloak user access could not be verified") from exc
        raise HTTPException(503, "Unable to verify current Keycloak membership") from exc
    return [group["path"] for group in groups if isinstance(group.get("path"), str)]


def _ensure_realm_role(admin: str, token: str, name: str, description: str = "") -> dict:
    try:
        status, role = _req("GET", f"{admin}/roles/{urllib.parse.quote(name)}", token=token)
        if status == 200 and isinstance(role, dict):
            return role
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        # Role missing — create below.
    try:
        _req(
            "POST",
            f"{admin}/roles",
            token=token,
            body={"name": name, "description": description or name},
        )
    except HTTPException as exc:
        # 409 conflict = already created concurrently; re-get below.
        if exc.status_code not in (409,):
            raise
    try:
        status, role = _req("GET", f"{admin}/roles/{urllib.parse.quote(name)}", token=token)
    except HTTPException as exc:
        raise HTTPException(502, f"Unable to ensure realm role {name}: {exc.detail}") from exc
    if status != 200 or not isinstance(role, dict):
        raise HTTPException(502, f"Unable to ensure realm role {name}")
    return role


def _get_group(admin: str, token: str, group_id: str) -> dict:
    status, group = _req("GET", f"{admin}/groups/{group_id}", token=token)
    if status != 200 or not isinstance(group, dict):
        raise HTTPException(502, "Unable to load Keycloak group")
    return group


def _get_children(admin: str, token: str, parent_id: str) -> list[dict]:
    """List a group's direct children via the dedicated endpoint.

    Not via ``parent.get("subGroups")`` on the parent's own representation —
    on this Keycloak version that field comes back empty/unreliable even when
    children genuinely exist (a known API change: subgroup listing moved to
    ``GET /groups/{id}/children``). Trusting the embedded field caused real
    child groups created by realm import to look "missing", so a create was
    attempted, hit 409 (already exists), and then verification failed the
    same way — surfacing as "Failed to create child group <name>" even
    though the group was there all along.
    """
    return _list_keycloak(f"{admin}/groups/{parent_id}/children", token)


def _find_child(admin: str, token: str, parent_id: str, name: str) -> dict | None:
    for child in _get_children(admin, token, parent_id):
        if isinstance(child, dict) and child.get("name") == name:
            return child
    return None


def _ensure_child_group(admin: str, token: str, parent_id: str | None, name: str) -> dict:
    if parent_id is None:
        for g in _list_keycloak(f"{admin}/groups", token):
            if g.get("name") == name:
                return _get_group(admin, token, g["id"])
        try:
            _req("POST", f"{admin}/groups", token=token, body={"name": name})
        except HTTPException as exc:
            if exc.status_code not in (409,):
                raise
        for g in _list_keycloak(f"{admin}/groups", token):
            if g.get("name") == name:
                return _get_group(admin, token, g["id"])
        raise HTTPException(502, f"Failed to create group {name}")

    found = _find_child(admin, token, parent_id, name)
    if found:
        return _get_group(admin, token, found["id"])
    try:
        _req("POST", f"{admin}/groups/{parent_id}/children", token=token, body={"name": name})
    except HTTPException as exc:
        if exc.status_code not in (409,):
            raise
    found = _find_child(admin, token, parent_id, name)
    if not found:
        raise HTTPException(502, f"Failed to create child group {name}")
    return _get_group(admin, token, found["id"])


def _map_role_on_group(admin: str, token: str, group_id: str, role: dict) -> None:
    status, existing = _req("GET", f"{admin}/groups/{group_id}/role-mappings/realm", token=token)
    names = {r.get("name") for r in (existing or []) if isinstance(r, dict)}
    if role["name"] in names:
        return
    _req(
        "POST",
        f"{admin}/groups/{group_id}/role-mappings/realm",
        token=token,
        body=[{"id": role["id"], "name": role["name"]}],
    )


def _provider_group(admin: str, token: str, provider: str, *, create: bool = False) -> dict:
    name = provider.upper()
    root = provider_group_root()
    if create:
        providers = _ensure_child_group(admin, token, None, root)
        return _ensure_child_group(admin, token, providers["id"], name)
    _, group = _req("GET", f"{admin}/group-by-path/{root}/{name}", token=token)
    if not isinstance(group, dict) or group.get("path") != f"/{root}/{name}" or not group.get("id"):
        raise HTTPException(502, "Invalid Keycloak provider group")
    return group


def create_provider_group(provider: str) -> dict:
    """Idempotently create the Keycloak group for a provider code."""
    cfg = require_admin_config()
    return _provider_group(_admin_root(cfg), _admin_token(cfg), provider, create=True)


def find_verified_user_id(email: str) -> str:
    """Resolve one existing and verified account for a provider grant."""
    cfg = require_admin_config()
    params = urllib.parse.urlencode({"email": email, "exact": "true"})
    _, users = _req("GET", f"{_admin_root(cfg)}/users?{params}", token=_admin_token(cfg))
    if not isinstance(users, list):
        raise HTTPException(502, "Invalid Keycloak account lookup response")
    verified = [user for user in users if (
        str(user.get("email", "")).lower() == email.lower()
        and user.get("emailVerified") is True and user.get("enabled") is True
    )]
    if len(verified) != 1 or not verified[0].get("id"):
        raise HTTPException(404, "An active, verified Keycloak account is required")
    return verified[0]["id"]


def find_webhook_service_subject(client_id: str) -> str:
    """Return the enabled service account for an existing confidential client."""
    cfg = require_admin_config()
    admin, token = _admin_root(cfg), _admin_token(cfg)
    params = urllib.parse.urlencode({"clientId": client_id, "search": "false", "max": 2})
    _, rows = _req("GET", f"{admin}/clients?{params}", token=token)
    if not isinstance(rows, list):
        raise HTTPException(502, "Invalid Keycloak client lookup response")
    clients = [row for row in rows if isinstance(row, dict) and row.get("clientId") == client_id]
    if len(clients) != 1:
        raise HTTPException(404, "Keycloak webhook client not found")
    client = clients[0]
    if (client.get("enabled") is not True or client.get("serviceAccountsEnabled") is not True
            or client.get("publicClient") is not False or not client.get("id")):
        raise HTTPException(422, "The webhook client must be confidential with an enabled service account")
    client_uuid = urllib.parse.quote(str(client["id"]), safe="")
    _, account = _req("GET", f"{admin}/clients/{client_uuid}/service-account-user", token=token)
    if not isinstance(account, dict) or account.get("enabled") is not True or not account.get("id"):
        raise HTTPException(422, "An active Keycloak service account is required")
    return str(account["id"])


def set_provider_membership(
    provider: str, user_id: str, role: str | None, *, allow_admin_changes: bool,
) -> dict:
    """Set or remove one provider role, leaving all other scopes in place."""
    if role is not None:
        role = normalize_role(role)
        if role not in STATE_ROLES:
            raise HTTPException(400, "Invalid provider role")
    cfg = require_admin_config()
    token, admin = _admin_token(cfg), _admin_root(cfg)
    provider_group = _provider_group(admin, token, provider)
    user_url = _user_admin_url(admin, user_id)
    groups_url = f"{user_url}/groups"
    current = _list_keycloak(groups_url, token)
    provider_groups = [group for group in current if provider in parse_group_paths([group.get("path")]).state_roles]
    if not allow_admin_changes and (
        role == ROLE_STATE_ADMIN
        or any(parse_group_paths([group.get("path")]).state_roles[provider] == ROLE_STATE_ADMIN for group in provider_groups)
    ):
        raise HTTPException(403, "Only a platform administrator can manage provider administrators")

    desired_group_id = None
    if role:
        group = _ensure_child_group(admin, token, provider_group["id"], group_leaf_for_role(role))
        desired_group_id = group["id"]
        if all(existing["id"] != desired_group_id for existing in provider_groups):
            _req("PUT", f"{groups_url}/{desired_group_id}", token=token)
    for existing in provider_groups:
        if existing["id"] != desired_group_id:
            _req("DELETE", f"{groups_url}/{existing['id']}", token=token)
    final_groups = _list_keycloak(groups_url, token)
    final_ids = {group["id"] for group in final_groups if provider in parse_group_paths([group.get("path")]).state_roles}
    if final_ids != ({desired_group_id} if desired_group_id else set()):
        raise HTTPException(502, "Keycloak provider membership change could not be confirmed")
    return {"provider": provider, "user_id": user_id, "role": role}


def list_provider_memberships(provider: str) -> list[dict]:
    """List only the requested provider's users and provider roles."""
    cfg = require_admin_config()
    token, admin = _admin_token(cfg), _admin_root(cfg)
    provider_group = _provider_group(admin, token, provider)
    groups = [provider_group, *_get_children(admin, token, provider_group["id"])]
    members: dict[str, dict] = {}
    for group in groups:
        role = parse_group_paths([group.get("path")]).state_roles.get(provider)
        if not role:
            continue
        for member in _list_keycloak(f"{admin}/groups/{group['id']}/members", token):
            user_id = member["id"]
            current = members.get(user_id)
            if current is None or STATE_ROLES.index(role) < STATE_ROLES.index(current["role"]):
                members[user_id] = {
                    "user_id": user_id, "role": role, "email": member.get("email") or "",
                    "username": member.get("username") or "", "enabled": member.get("enabled") is True,
                }
    return sorted(members.values(), key=lambda member: (member["email"], member["user_id"]))


def ensure_access_group(
    cfg: KeycloakAdminConfig,
    *,
    access_type: str,
    state: str | None,
    role: str | None,
) -> tuple[str, dict]:
    """Ensure group exists; return (path, group_rep)."""
    token = _admin_token(cfg)
    admin = _admin_root(cfg)
    access_type = (access_type or "").strip().lower()

    if access_type in GLOBAL_VIEWER_ROLE_ALIASES:
        viewer_role = _ensure_realm_role(
            admin, token, ROLE_GLOBAL_VIEWER, "Viewer with read-only access to all providers"
        )
        global_g = _ensure_child_group(admin, token, None, "global")
        leaf = _ensure_child_group(admin, token, global_g["id"], group_leaf_for_role(ROLE_GLOBAL_VIEWER))
        leaf_full = _get_group(admin, token, leaf["id"])
        _map_role_on_group(admin, token, leaf_full["id"], viewer_role)
        return leaf_full.get("path") or GLOBAL_VIEWER_GROUP_PATH, leaf_full

    if access_type in _SUPER_ADMIN_ACCESS_TYPES:
        sa_role = _ensure_realm_role(
            admin, token, ROLE_SUPER_ADMIN, "Platform super admin — all states"
        )
        global_g = _ensure_child_group(admin, token, None, "global")
        # Prefer nested super-admin; also accept flat name used in some installs
        leaf = _find_child(admin, token, global_g["id"], "super-admin")
        if not leaf:
            # Flat path group named global/super-admin
            status, top = _req("GET", f"{admin}/groups?max=200", token=token)
            for g in top or []:
                if g.get("path") == "/global/super-admin" or g.get("name") == "global/super-admin":
                    leaf_full = _get_group(admin, token, g["id"])
                    _map_role_on_group(admin, token, leaf_full["id"], sa_role)
                    return leaf_full.get("path") or "/global/super-admin", leaf_full
            leaf = _ensure_child_group(admin, token, global_g["id"], "super-admin")
        leaf_full = _get_group(admin, token, leaf["id"] if isinstance(leaf, dict) else leaf)
        _map_role_on_group(admin, token, leaf_full["id"], sa_role)
        return leaf_full.get("path") or "/global/super-admin", leaf_full

    state_code = (state or "").strip().upper()
    role_canon = normalize_role(role) or (role or "").strip().lower()
    if not re.fullmatch(r"[A-Z0-9](?:[A-Z0-9_-]{0,61}[A-Z0-9])?", state_code):
        raise HTTPException(400, "Invalid provider code")
    if role_canon not in STATE_ROLES:
        raise HTTPException(
            400,
            "role must be one of: " + ", ".join(STATE_ROLES),
        )

    realm_role = _ensure_realm_role(
        admin,
        token,
        role_canon,
        STATE_ROLE_DESCRIPTIONS[role_canon],
    )
    # Group leaf: /states/MH/admin or /states/MH/view
    leaf_name = group_leaf_for_role(role_canon)
    state_g = _provider_group(admin, token, state_code, create=False)
    leaf = _ensure_child_group(admin, token, state_g["id"], leaf_name)
    _map_role_on_group(admin, token, leaf["id"], realm_role)
    path = leaf.get("path") or f"/{provider_group_root()}/{state_code}/{leaf_name}"
    return path, leaf


def _username_from_email(email: str) -> str:
    local = email.split("@", 1)[0].strip().lower()
    local = re.sub(r"[^a-z0-9._-]+", "", local) or "user"
    return local[:64]


def provision_user(
    *,
    email: str,
    first_name: str = "",
    last_name: str = "",
    username: str | None = None,
    access_type: str,
    state: str | None = None,
    role: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Create or update Keycloak user and join the access group."""
    cfg = require_admin_config()
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Valid email is required (used for Google SSO)")

    group_path, group = ensure_access_group(
        cfg, access_type=access_type, state=state, role=role
    )
    token = _admin_token(cfg)
    admin = _admin_root(cfg)

    uname = (username or "").strip() or _username_from_email(email)
    # Find by email
    status, found = _req(
        "GET",
        f"{admin}/users?{urllib.parse.urlencode({'email': email, 'exact': 'true'})}",
        token=token,
    )
    body = {
        "username": uname,
        "email": email,
        "enabled": bool(enabled),
        "emailVerified": True,
        "firstName": (first_name or "").strip() or None,
        "lastName": (last_name or "").strip() or None,
        "requiredActions": [],
    }
    # Keycloak rejects null names sometimes — omit empty
    body = {k: v for k, v in body.items() if v is not None}

    created = False
    if isinstance(found, list) and found:
        uid = found[0]["id"]
        body["username"] = found[0].get("username") or uname
        actions = found[0].get("requiredActions")
        if isinstance(actions, list):
            body["requiredActions"] = [action for action in actions if action != "UPDATE_PASSWORD"]
        else:
            body.pop("requiredActions")
        _req("PUT", f"{admin}/users/{uid}", token=token, body=body)
    else:
        try:
            _req("POST", f"{admin}/users", token=token, body=body)
            created = True
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
        status, found = _req(
            "GET",
            f"{admin}/users?{urllib.parse.urlencode({'email': email, 'exact': 'true'})}",
            token=token,
        )
        if not found:
            status, found = _req(
                "GET",
                f"{admin}/users?{urllib.parse.urlencode({'username': uname, 'exact': 'true'})}",
                token=token,
            )
        if not found:
            raise HTTPException(502, "User created but could not be loaded")
        uid = found[0]["id"]

    # Join group
    try:
        _req("PUT", f"{admin}/users/{uid}/groups/{group['id']}", token=token)
    except HTTPException:
        raise

    # Group membership is the ONLY source of roles. The realm role is already
    # mapped onto the leaf group by ``ensure_access_group``, so it reaches the
    # JWT through the group. Deliberately no direct user→role mapping here:
    # direct assignments produce users who carry permissions with no tenant
    # scope, and they are invisible in the group tree.
    access_key = (access_type or "").strip().lower()
    if access_key in _SUPER_ADMIN_ACCESS_TYPES:
        role_name = ROLE_SUPER_ADMIN
    elif access_key in GLOBAL_VIEWER_ROLE_ALIASES:
        role_name = ROLE_GLOBAL_VIEWER
    else:
        role_name = normalize_role(role) or (role or "").strip().lower()

    status, user = _req("GET", f"{admin}/users/{uid}", token=token)
    status, groups = _req("GET", f"{admin}/users/{uid}/groups", token=token)
    status, roles = _req("GET", f"{admin}/users/{uid}/role-mappings/realm", token=token)

    ui_url = (os.environ.get("DOCS_PIPELINE_UI_URL") or "http://localhost:3001").rstrip("/")
    login_url = f"{ui_url}/login" if not ui_url.endswith("/login") else ui_url

    if role_name == ROLE_SUPER_ADMIN:
        access_label = "Platform administrator"
    elif role_name == ROLE_GLOBAL_VIEWER:
        access_label = "Viewer"
    else:
        access_label = f"{(state or '').upper()} · {(role or '').capitalize()}"

    share_lines = [
        "You have been given access to the Docs Pipeline console.",
        "",
        f"App URL: {ui_url}",
        f"Login page: {login_url}",
        "Sign-in: Continue with SSO (Google)",
        f"Use this Google account email: {email}",
        "",
        f"Access level: {access_label}",
        f"Keycloak group: {group_path}",
        "",
        "Steps:",
        "1. Open the app URL",
        "2. Click Continue with SSO",
        "3. Choose the Google account with the email above",
        "4. After first login, you should land on the dashboard",
        "",
        "If sign-in fails, contact your platform administrator.",
    ]

    return {
        "created": created,
        "user_id": uid,
        "username": (user or {}).get("username") or uname,
        "email": email,
        "first_name": (user or {}).get("firstName") or first_name,
        "last_name": (user or {}).get("lastName") or last_name,
        "enabled": bool((user or {}).get("enabled", enabled)),
        "email_verified": bool((user or {}).get("emailVerified", True)),
        "access_type": (
            "super_admin"
            if role_name == ROLE_SUPER_ADMIN
            else ROLE_GLOBAL_VIEWER
            if role_name == ROLE_GLOBAL_VIEWER
            else "state"
        ),
        "state": (state or "").upper() or None,
        "role": role_name,
        "group_path": group_path,
        "groups": [g.get("path") for g in (groups or []) if isinstance(g, dict)],
        "roles": [r.get("name") for r in (roles or []) if isinstance(r, dict)],
        "app_url": ui_url,
        "login_url": login_url,
        "portal_instance": portal_instance(),
        "share_message": "\n".join(share_lines),
    }


def list_provider_groups(cfg: KeycloakAdminConfig | None = None) -> list[dict] | None:
    """Return the realm's direct provider groups, or None without an admin client."""
    cfg = cfg or load_keycloak_admin_config()
    if not cfg.configured:
        return None
    root_name = provider_group_root()
    admin, token = _admin_root(cfg), _admin_token(cfg)
    root = next((group for group in _list_keycloak(f"{admin}/groups", token) if group.get("name") == root_name), None)
    if root is None:
        return []
    return [
        group for group in _get_children(admin, token, root["id"])
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", group.get("name", ""))
    ]


def list_access_options() -> dict[str, Any]:
    """List the provider groups that exist in this Keycloak realm."""
    cfg = load_keycloak_admin_config()
    root_name = provider_group_root()
    providers = [
        {"code": group["name"].upper(), "label": group["name"]}
        for group in list_provider_groups(cfg) or []
    ]
    return {
        "keycloak_admin_configured": cfg.configured,
        "realm": cfg.realm,
        "portal_instance": portal_instance(),
        "access_types": [
            {
                "id": "super_admin",
                "label": "Platform administrator",
                "description": "Full access to all states, settings, user management, and PROD approval",
                "group": "/global/super-admin",
            },
            {
                "id": ROLE_GLOBAL_VIEWER,
                "label": "Viewer",
                "description": "Read-only access across all providers.",
                "group": GLOBAL_VIEWER_GROUP_PATH,
            },
            {
                "id": "state",
                "label": "Provider role",
                "description": (
                    "Access limited to one state or centre, as Admin, Approver, "
                    "Contributor, or View"
                ),
                "group_template": f"/{root_name}/{{PROVIDER}}/{{admin|approver|contributor|view}}",
            },
        ],
        "state_roles": [
            {
                "id": ROLE_STATE_ADMIN,
                "label": "Admin",
                "description": (
                    "Everything in the state: upload, edit, review & approve, "
                    "run pipeline, delete own documents"
                ),
            },
            {
                "id": ROLE_STATE_APPROVER,
                "label": "Approver",
                "description": (
                    "Upload, edit, review & approve, run pipeline. Cannot delete."
                ),
            },
            {
                "id": ROLE_STATE_CONTRIBUTOR,
                "label": "Contributor",
                "description": (
                    "Upload and run pipeline. Cannot edit, approve, or delete."
                ),
            },
            {
                "id": ROLE_STATE_VIEW,
                "label": "View",
                "description": "View-only access within the state (search / browse)",
            },
        ],
        "states": sorted(providers, key=lambda provider: provider["code"]),
        "required_fields": {
            "super_admin": ["email", "first_name", "last_name"],
            ROLE_GLOBAL_VIEWER: ["email", "first_name", "last_name"],
            "state": ["email", "first_name", "last_name", "state", "role"],
        },
        "notes": [
            "Users sign in with Google SSO using the same email you enter.",
            "No app password is required for normal SSO login.",
            "Provider codes match the instance codes used by documents.",
            "Access comes from group membership only — no direct role assignment.",
            f"Keycloak groups: /global/super-admin, {GLOBAL_VIEWER_GROUP_PATH}, "
            f"/{root_name}/{{PROVIDER}}/{{admin|approver|contributor|view}}",
        ],
    }


def _is_product_group_path(path: str) -> bool:
    """True when a group path grants a product role.

    ``/global/super-admin``, ``/global/global-viewer``, or ``/states/{CODE}/{leaf}``.
    Anything else (org units, ad-hoc groups) is left alone when changing a role.
    """
    return bool(parse_group_paths([path]).roles)


def set_user_access(
    *,
    user_id: str,
    access_type: str,
    state: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """Update the user's role in the selected provider or platform scope."""
    cfg = require_admin_config()
    token = _admin_token(cfg)
    admin = _admin_root(cfg)

    user_url = _user_admin_url(admin, user_id)
    status, target_user = _req("GET", user_url, token=token)
    if status != 200 or not target_user:
        raise HTTPException(404, "User not found")

    path, group = ensure_access_group(cfg, access_type=access_type, state=state, role=role)

    _req("PUT", f"{user_url}/groups/{group['id']}", token=token)

    target_scope = parse_group_paths([path]).state_roles.keys()
    current = _list_keycloak(f"{user_url}/groups", token)
    removed: list[str] = []
    for existing in current or []:
        existing_path = existing.get("path") or ""
        if existing_path.rstrip("/") == path.rstrip("/"):
            continue
        if not _is_product_group_path(existing_path):
            continue
        existing_scope = parse_group_paths([existing_path]).state_roles.keys()
        if existing_scope != target_scope:
            continue
        _req("DELETE", f"{user_url}/groups/{existing['id']}", token=token)
        removed.append(existing_path)

    # Groups are the only source of roles.
    _, mapped = _req("GET", f"{user_url}/role-mappings/realm", token=token)
    stripped: list[str] = []
    for realm_role in mapped or []:
        name = realm_role.get("name") or ""
        if not normalize_role(name):
            continue
        _req(
            "DELETE",
            f"{user_url}/role-mappings/realm",
            token=token,
            body=[{"id": realm_role["id"], "name": name}],
        )
        stripped.append(name)

    after = _list_keycloak(f"{user_url}/groups", token)
    paths = [g.get("path") or "" for g in (after or [])]
    summary = _access_summary_from_groups(paths)

    return {
        "user_id": user_id,
        "email": target_user.get("email") or "",
        "username": target_user.get("username") or "",
        "group": path,
        "groups": paths,
        "removed_groups": removed,
        "removed_direct_roles": stripped,
        **summary,
        "requires_relogin": True,
        "note": "The user must sign out and back in for the new role to take effect.",
    }


def _access_summary_from_groups(group_paths: list[str]) -> dict[str, Any]:
    """Derive display access from Keycloak group paths.

    Users without product groups (super_admin / state roles) are labeled
    **Dashboard** — baseline SSO access (search/view only), not an error state.
    """
    access = parse_group_paths(group_paths)
    if access.is_super_admin:
        return {
            "access_type": "super_admin",
            "access_label": "Super Admin",
            "states": [],
            "roles": [ROLE_SUPER_ADMIN],
        }
    if access.is_global_viewer:
        return {
            "access_type": ROLE_GLOBAL_VIEWER,
            "access_label": "Viewer",
            "states": [],
            "roles": access.roles,
            "provider_roles": dict(access.state_roles),
        }
    if access.state_roles:
        memberships = sorted(access.state_roles.items())
        return {
            "access_type": "state",
            "access_label": ", ".join(f"{provider.upper()} · {role}" for provider, role in memberships),
            "states": [provider.upper() for provider, _ in memberships],
            "roles": access.roles,
            "provider_roles": dict(access.state_roles),
        }

    # No product group → baseline dashboard / search-only access after SSO
    return {
        "access_type": "dashboard",
        "access_label": "Dashboard",
        "states": [],
        "roles": [],
    }


def list_realm_users(*, search: str = "", max_results: int = 100) -> dict[str, Any]:
    """List users in the SSO realm with groups (no delete)."""
    cfg = require_admin_config()
    token = _admin_token(cfg)
    admin = _admin_root(cfg)
    max_results = max(1, min(int(max_results or 100), 200))
    params: dict[str, Any] = {"max": max_results, "briefRepresentation": "false"}
    if (search or "").strip():
        params["search"] = search.strip()
    status, users = _req(
        "GET",
        f"{admin}/users?{urllib.parse.urlencode(params)}",
        token=token,
    )
    if not isinstance(users, list):
        raise HTTPException(502, "Unable to list Keycloak users")

    rows: list[dict[str, Any]] = []
    for u in users:
        if not isinstance(u, dict):
            continue
        uid = u.get("id")
        if not uid:
            continue
        try:
            _, groups = _req("GET", f"{admin}/users/{uid}/groups", token=token)
        except HTTPException:
            groups = []
        paths = [g.get("path") for g in (groups or []) if isinstance(g, dict) and g.get("path")]
        summary = _access_summary_from_groups(paths)
        first = (u.get("firstName") or "").strip()
        last = (u.get("lastName") or "").strip()
        name = f"{first} {last}".strip() or (u.get("username") or "")
        rows.append(
            {
                "user_id": uid,
                "username": u.get("username") or "",
                "email": u.get("email") or "",
                "name": name,
                "first_name": first,
                "last_name": last,
                "enabled": bool(u.get("enabled")),
                "email_verified": bool(u.get("emailVerified")),
                "groups": paths,
                "access_type": summary["access_type"],
                "access_label": summary["access_label"],
                "states": summary["states"],
                "roles": summary["roles"],
                "provider_roles": summary.get("provider_roles", {}),
            }
        )

    # Prefer users with product groups first, then by email
    def sort_key(row: dict) -> tuple:
        rank = 0 if row.get("access_type") in ("super_admin", ROLE_GLOBAL_VIEWER, "state") else 1
        return (rank, (row.get("email") or row.get("username") or "").lower())

    rows.sort(key=sort_key)
    return {
        "total": len(rows),
        "users": rows,
        "realm": cfg.realm,
    }
