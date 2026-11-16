"""Keycloak token exchange and refresh for the confidential console client."""

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


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    backchannel_url: str = ""

    @property
    def token_url(self) -> str:
        return f"{self.backchannel_url or self.issuer}/protocol/openid-connect/token"


def require_oidc_config() -> OidcConfig:
    issuer = os.environ.get("KEYCLOAK_ISSUER", "").strip().rstrip("/")
    client_id = os.environ.get("KEYCLOAK_CLIENT_ID", "").strip()
    client_secret = os.environ.get("KEYCLOAK_CLIENT_SECRET", "")
    if not (issuer and client_id and client_secret):
        raise HTTPException(
            503, "Keycloak sign-in requires KEYCLOAK_ISSUER, KEYCLOAK_CLIENT_ID, and KEYCLOAK_CLIENT_SECRET."
        )
    return OidcConfig(
        issuer=issuer, client_id=client_id, client_secret=client_secret,
        backchannel_url=os.environ.get("KEYCLOAK_BACKCHANNEL_URL", "").strip().rstrip("/"),
    )


def _post(url: str, *, form: dict[str, str]) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode(),
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response) if response.status != 204 else None
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except (ValueError, OSError):
            return exc.code, None
    except (ValueError, OSError) as exc:
        raise HTTPException(502, "Keycloak is temporarily unavailable.") from exc


def _token_response(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "id_token": payload.get("id_token"),
        "token_type": payload.get("token_type", "Bearer"),
        "expires_in": payload.get("expires_in"),
        "refresh_expires_in": payload.get("refresh_expires_in"),
    }


def issue_service_token(client_id: str, client_secret: str) -> dict[str, Any]:
    """Obtain a short-lived client credentials token through the backchannel."""
    status, body = _post(require_oidc_config().token_url, form={
        "grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret,
    })
    if status == 200 and isinstance(body, dict) and isinstance(body.get("access_token"), str):
        return {"access_token": body["access_token"], "token_type": "Bearer", "expires_in": body.get("expires_in")}
    if status in (400, 401, 403):
        raise HTTPException(401, "Keycloak rejected the service client credentials.")
    raise HTTPException(502, "Keycloak could not issue a service token.")


def refresh_tokens(refresh_token: str) -> dict[str, Any]:
    """Renew a Keycloak session using the confidential client."""
    cfg = require_oidc_config()
    token = (refresh_token or "").strip()
    if not token:
        raise HTTPException(400, "Missing refresh token.")

    status, body = _post(
        cfg.token_url,
        form={
            "grant_type": "refresh_token",
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "refresh_token": token,
        },
    )
    payload = body if isinstance(body, dict) else {}

    if status == 200 and payload.get("access_token"):
        return _token_response(payload)

    # An expired or revoked refresh token is a normal end-of-session, not a fault.
    raise HTTPException(401, "Your session has expired. Please sign in again.")


def end_session(refresh_token: str) -> None:
    """Revoke the console session through the private Keycloak endpoint."""
    cfg = require_oidc_config()
    status, _ = _post(
        f"{cfg.backchannel_url or cfg.issuer}/protocol/openid-connect/logout",
        form={
            "client_id": cfg.client_id, "client_secret": cfg.client_secret,
            "refresh_token": refresh_token,
        },
    )
    if status not in (204, 400, 401):
        raise HTTPException(502, "Could not end the identity session.")


def exchange_authorization_code(
    code: str, redirect_uri: str, code_verifier: str | None = None
) -> dict[str, Any]:
    """Exchange a PKCE-bound authorization code using the confidential client."""
    cfg = require_oidc_config()
    authorization_code = (code or "").strip()
    if not authorization_code:
        raise HTTPException(400, "Missing authorization code.")
    if not (redirect_uri or "").strip():
        raise HTTPException(400, "Missing redirect_uri.")

    if code_verifier is None or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", code_verifier):
        raise HTTPException(400, "Missing or invalid PKCE verifier. Please start sign-in again.")

    form = {
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "code": authorization_code,
        "redirect_uri": redirect_uri,
    }

    status, body = _post(cfg.token_url, form=form)
    payload = body if isinstance(body, dict) else {}

    if status == 200 and payload.get("access_token"):
        return _token_response(payload)

    error = str(payload.get("error") or "")

    if error == "invalid_client" or error == "unauthorized_client":
        raise HTTPException(
            503,
            "SSO is misconfigured (Keycloak rejected the client credentials). Check "
            "KEYCLOAK_CLIENT_ID / KEYCLOAK_CLIENT_SECRET.",
        )
    if error == "invalid_grant":
        # A code is single-use and short-lived; a reload of the callback URL
        # replays a spent one, which is the common way to land here.
        raise HTTPException(401, "That sign-in link has already been used or expired. Try again.")

    raise HTTPException(
        502 if status >= 500 else 400,
        "Could not complete sign-in.",
    )
