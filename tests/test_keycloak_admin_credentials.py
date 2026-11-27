import pytest

from pipeline.auth import keycloak_admin

ADMIN_ENV = (
    "KEYCLOAK_ISSUER",
    "KEYCLOAK_ADMIN_BASE_URL",
    "KEYCLOAK_ADMIN_REALM",
    "KEYCLOAK_ADMIN_TOKEN_REALM",
    "KEYCLOAK_ADMIN_CLIENT_ID",
    "KEYCLOAK_ADMIN_CLIENT_SECRET",
    "KEYCLOAK_ADMIN_USERNAME",
    "KEYCLOAK_ADMIN_PASSWORD",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ADMIN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KEYCLOAK_ADMIN_BASE_URL", "http://keycloak:8080/auth")
    monkeypatch.setenv("KEYCLOAK_ADMIN_REALM", "knowledge-engine")


def _capture_token_request(monkeypatch):
    seen = {}

    def fake_req(method, url, *, form=None, **_):
        seen.update(url=url, form=form)
        return 200, {"access_token": "t"}

    monkeypatch.setattr(keycloak_admin, "_req", fake_req)
    return seen


def test_service_account_uses_client_credentials_in_managed_realm(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "kp-admin-api")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "s3cret")
    seen = _capture_token_request(monkeypatch)

    cfg = keycloak_admin.load_keycloak_admin_config()
    assert cfg.configured and not cfg.uses_password_grant
    assert keycloak_admin._admin_token(cfg) == "t"
    assert seen["url"].endswith("/realms/knowledge-engine/protocol/openid-connect/token")
    assert seen["form"]["grant_type"] == "client_credentials"
    assert seen["form"]["client_id"] == "kp-admin-api"


def test_admin_login_uses_master_realm_admin_cli(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ADMIN_USERNAME", "kp-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_PASSWORD", "pw")
    seen = _capture_token_request(monkeypatch)

    cfg = keycloak_admin.load_keycloak_admin_config()
    assert cfg.configured and cfg.uses_password_grant
    assert keycloak_admin._admin_token(cfg) == "t"
    assert seen["url"].endswith("/realms/master/protocol/openid-connect/token")
    assert seen["form"] == {
        "grant_type": "password",
        "client_id": "admin-cli",
        "username": "kp-admin",
        "password": "pw",
    }


def test_admin_login_wins_when_both_are_set(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "kp-admin-api")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("KEYCLOAK_ADMIN_USERNAME", "kp-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_PASSWORD", "pw")
    assert keycloak_admin.load_keycloak_admin_config().uses_password_grant


def test_token_realm_override_applies_to_admin_login(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ADMIN_USERNAME", "kp-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("KEYCLOAK_ADMIN_TOKEN_REALM", "ops")
    assert keycloak_admin.load_keycloak_admin_config().token_realm == "ops"


def test_half_configured_credentials_are_not_configured(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ADMIN_USERNAME", "kp-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "kp-admin-api")
    assert not keycloak_admin.load_keycloak_admin_config().configured


def test_password_is_not_in_repr(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ADMIN_USERNAME", "kp-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_PASSWORD", "pw-value-123")
    assert "pw-value-123" not in repr(keycloak_admin.load_keycloak_admin_config())
