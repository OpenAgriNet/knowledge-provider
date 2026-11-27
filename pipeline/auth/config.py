"""Auth configuration loaded from environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AuthConfig:
    """
    Production requires a Keycloak bearer token addressed to this API.
    Local fixtures can opt into a synthetic admin explicitly.
    """

    disabled: bool
    keycloak_issuer: str
    keycloak_audience: str
    keycloak_jwks_url: str
    # Clock-skew tolerance when validating exp/nbf (seconds).
    jwt_leeway_seconds: int = 30
    verify_live_groups: bool = False
    runtime_profile: str = "production"


def load_auth_config() -> AuthConfig:
    issuer = (os.environ.get("KEYCLOAK_ISSUER") or "").rstrip("/")
    jwks_url = (os.environ.get("KEYCLOAK_JWKS_URL") or "").strip()
    if not jwks_url and issuer:
        jwks_url = f"{issuer}/protocol/openid-connect/certs"

    leeway_raw = (os.environ.get("KEYCLOAK_JWT_LEEWAY_SECONDS") or "30").strip()
    try:
        leeway = max(0, int(leeway_raw))
    except ValueError:
        leeway = 30

    audience = os.environ.get("KEYCLOAK_AUDIENCE", "").strip()

    return AuthConfig(
        disabled=_env_bool("AUTH_DISABLED", False),
        keycloak_issuer=issuer,
        keycloak_audience=audience,
        keycloak_jwks_url=jwks_url,
        jwt_leeway_seconds=leeway,
        verify_live_groups=_env_bool("KEYCLOAK_VERIFY_LIVE_GROUPS", False),
        runtime_profile=os.environ.get("KP_RUNTIME_PROFILE", "production").strip().lower(),
    )


def validate_auth_config(config: AuthConfig) -> None:
    """Fail startup early when enabled auth cannot validate Keycloak tokens."""
    if config.disabled:
        if config.runtime_profile != "local":
            raise RuntimeError("AUTH_DISABLED=true requires KP_RUNTIME_PROFILE=local")
        return

    missing: list[str] = []
    if not config.keycloak_issuer:
        missing.append("KEYCLOAK_ISSUER")
    if not config.keycloak_jwks_url:
        missing.append("KEYCLOAK_JWKS_URL")
    if not config.keycloak_audience:
        missing.append("KEYCLOAK_AUDIENCE")
    if missing:
        raise RuntimeError(
            "AUTH_DISABLED=false requires Keycloak configuration: "
            + ", ".join(missing)
        )
    if config.verify_live_groups:
        from .keycloak_admin import load_keycloak_admin_config

        if not load_keycloak_admin_config().configured:
            raise RuntimeError("KEYCLOAK_VERIFY_LIVE_GROUPS=true requires Keycloak admin access")
