#!/usr/bin/env python3
"""Ensure docs-pipeline Keycloak clients and claims with a service account.

Optionally invite a test operator to enroll a passkey.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def _req(method: str, url: str, *, token: str | None = None, body=None, form=None):
    headers = {}
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
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        detail = raw.decode("utf-8", errors="replace")
        if exc.code == 409:
            return exc.code, None
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("KEYCLOAK_BASE_URL", "http://127.0.0.1:8082/auth"))
    parser.add_argument("--realm", default="docs-pipeline")
    parser.add_argument("--redirect-uri", default="http://localhost:3000/auth/sso-callback")
    parser.add_argument("--test-username", default="docs-test-curator")
    parser.add_argument("--test-email", help="Create a test operator and send a passkey enrollment link")
    parser.add_argument("--test-group", help="Optional existing Keycloak group path for the test operator")
    args = parser.parse_args()
    admin_client = os.environ.get("KEYCLOAK_ADMIN_CLIENT_ID")
    admin_secret = os.environ.get("KEYCLOAK_ADMIN_CLIENT_SECRET")
    if not admin_client or not admin_secret:
        print("KEYCLOAK_ADMIN_CLIENT_ID and KEYCLOAK_ADMIN_CLIENT_SECRET are required", file=sys.stderr)
        return 2

    base = args.base_url.rstrip("/")
    token_realm = os.environ.get("KEYCLOAK_ADMIN_TOKEN_REALM") or args.realm
    _, token_body = _req(
        "POST",
        f"{base}/realms/{token_realm}/protocol/openid-connect/token",
        form={
            "grant_type": "client_credentials",
            "client_id": admin_client,
            "client_secret": admin_secret,
        },
    )
    token = token_body["access_token"]
    admin = f"{base}/admin/realms/{args.realm}"

    def ensure_client(client_id: str, rep: dict) -> str:
        _, found = _req("GET", f"{admin}/clients?{urllib.parse.urlencode({'clientId': client_id})}", token=token)
        if found:
            if rep.get("standardFlowEnabled") and (found[0].get("publicClient") or found[0].get("directAccessGrantsEnabled")):
                raise RuntimeError("Configure the existing console client for confidential PKCE sign-in before bootstrapping")
            return found[0]["id"]
        _req("POST", f"{admin}/clients", token=token, body=rep)
        _, found = _req("GET", f"{admin}/clients?{urllib.parse.urlencode({'clientId': client_id})}", token=token)
        return found[0]["id"]

    ensure_client(
        "docs-pipeline-api",
        {
            "clientId": "docs-pipeline-api",
            "enabled": True,
            "protocol": "openid-connect",
            "bearerOnly": True,
            "publicClient": False,
        },
    )
    ui_id = ensure_client(
        "docs-pipeline-ui",
        {
            "clientId": "docs-pipeline-ui",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,
            "implicitFlowEnabled": False,
            "redirectUris": [args.redirect_uri],
            "attributes": {"pkce.code.challenge.method": "S256"},
        },
    )

    mappers = [
        {
            "name": "docs-pipeline-api-audience",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.client.audience": "docs-pipeline-api",
                "access.token.claim": "true",
                "id.token.claim": "false",
            },
        },
        {
            "name": "groups",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper",
            "config": {
                "claim.name": "groups",
                "full.path": "true",
                "access.token.claim": "true",
            },
        },
    ]
    mappers_url = f"{admin}/clients/{ui_id}/protocol-mappers/models"
    _, existing = _req("GET", mappers_url, token=token)
    names = {item.get("name") for item in (existing or [])}
    for mapper in mappers:
        if mapper["name"] not in names:
            _req("POST", mappers_url, token=token, body=mapper)
    if args.test_email:
        invite_operator(admin, token, args.test_username, args.test_email, args.test_group)
    print(f"bootstrap=ok realm={args.realm}")
    return 0


def invite_operator(admin: str, token: str, username: str, email: str, group_path: str | None) -> None:
    group = None
    if group_path:
        path = urllib.parse.quote(group_path.strip("/"), safe="/")
        _, group = _req("GET", f"{admin}/group-by-path/{path}", token=token)
    users_url = f"{admin}/users?{urllib.parse.urlencode({'username': username, 'exact': 'true'})}"
    _, users = _req("GET", users_url, token=token)
    if users and (users[0].get("email") or "").casefold() != email.casefold():
        raise ValueError("The test username already belongs to a different email")
    if not users:
        _req("POST", f"{admin}/users", token=token, body={
            "username": username, "email": email, "enabled": True,
            "firstName": "Test", "lastName": "Operator",
        })
        _, users = _req("GET", users_url, token=token)
    user_url = f"{admin}/users/{users[0]['id']}"
    if group:
        _req("PUT", f"{user_url}/groups/{group['id']}", token=token)
    _req("PUT", f"{user_url}/execute-actions-email", token=token, body=["webauthn-register-passwordless"])


if __name__ == "__main__":
    raise SystemExit(main())
