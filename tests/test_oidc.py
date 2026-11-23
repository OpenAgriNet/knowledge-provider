"""Regression tests for Keycloak console session exchange."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from pipeline.auth import oidc
from pipeline.auth.keycloak_admin import load_keycloak_admin_config

ISSUER = "https://login.example/auth/realms/knowledge"
CALLBACK = "https://console.example/auth/sso-callback"
VERIFIER = "a" * 43


@pytest.fixture
def oidc_settings(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ISSUER", ISSUER)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "knowledge-console")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "test-console-secret")
    monkeypatch.setenv("KEYCLOAK_BACKCHANNEL_URL", "http://keycloak:8080/auth/realms/knowledge")


def test_exchange_uses_confidential_client_and_pkce(oidc_settings):
    with patch.object(oidc, "_post", return_value=(200, {"access_token": "access"})) as post:
        assert oidc.exchange_authorization_code("code", CALLBACK, VERIFIER)["access_token"] == "access"
    post.assert_called_once_with(
        "http://keycloak:8080/auth/realms/knowledge/protocol/openid-connect/token",
        form={
            "grant_type": "authorization_code",
            "client_id": "knowledge-console",
            "client_secret": "test-console-secret",
            "code": "code",
            "code_verifier": VERIFIER,
            "redirect_uri": CALLBACK,
        },
    )


@pytest.mark.parametrize("verifier", [None, "too-short", "!" * 43])
def test_exchange_rejects_invalid_pkce_before_contacting_keycloak(oidc_settings, verifier):
    with patch.object(oidc, "_post") as post, pytest.raises(HTTPException) as exc:
        oidc.exchange_authorization_code("code", CALLBACK, verifier)
    assert exc.value.status_code == 400
    post.assert_not_called()


def test_expired_refresh_is_an_unauthenticated_session(oidc_settings):
    with patch.object(oidc, "_post", return_value=(400, {"error": "invalid_grant"})) as post:
        with pytest.raises(HTTPException) as exc:
            oidc.refresh_tokens("stale")
    assert exc.value.status_code == 401
    assert post.call_args.kwargs["form"]["grant_type"] == "refresh_token"


def test_admin_requires_a_service_account_or_admin_login(monkeypatch, oidc_settings):
    for name in (
        "KEYCLOAK_ADMIN_CLIENT_ID",
        "KEYCLOAK_ADMIN_CLIENT_SECRET",
        "KEYCLOAK_ADMIN_USERNAME",
        "KEYCLOAK_ADMIN_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    assert not load_keycloak_admin_config().configured
