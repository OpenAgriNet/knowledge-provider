from urllib.parse import urlencode

import httpx
import pytest
from fastapi import HTTPException

from pipeline.auth import google_sign_in as google
from pipeline.auth.oidc import OidcConfig

ISSUER = "https://console.example.invalid/auth/realms/kp"
PRIVATE = "http://keycloak:8080/auth/realms/kp"
CONSOLE = google.GoogleConsole("https://console.example.invalid/knowledge-provider/login")


def test_google_exchange_stays_private_and_rejects_wrong_state(monkeypatch):
    config = OidcConfig(ISSUER, "console", "fixture-secret", PRIVATE)
    monkeypatch.setattr(google, "require_oidc_config", lambda: config)
    calls = []
    auth = {}

    def redirect(url):
        return httpx.Response(303, headers={"Location": url})

    def broker(request):
        assert str(request.url).startswith(PRIVATE)
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith("/auth"):
            auth.update(request.url.params)
            return httpx.Response(303, headers={
                "Location": ISSUER + "/broker/google/login?client_id=console",
                "Set-Cookie": "AUTH_SESSION_ID=fixture; Secure; HttpOnly; Path=/auth",
            })
        assert request.headers.get("Cookie") == "AUTH_SESSION_ID=fixture"
        if request.url.path.endswith("/login"):
            query = urlencode({
                "state": "google-state", "redirect_uri": ISSUER + "/broker/google/endpoint",
            })
            return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + query)
        assert request.url.params["code"] == "google-code"
        assert request.url.params["state"] == "google-state"
        return redirect(auth["redirect_uri"] + "?" + urlencode({
            "code": "keycloak-code", "state": auth["state"],
        }))

    monkeypatch.setattr(google, "private_client", lambda cookies=None: httpx.Client(
        transport=httpx.MockTransport(broker), cookies=cookies,
    ))

    def exchange(code, redirect_uri, verifier):
        assert code == "keycloak-code"
        assert redirect_uri == auth["redirect_uri"]
        assert len(verifier) >= 43
        return {"access_token": "fixture"}

    monkeypatch.setattr(google, "exchange_authorization_code", exchange)
    location, cookie = google.start_google_sign_in(CONSOLE)
    assert location.startswith("https://accounts.google.com/")
    assert "AUTH_SESSION_ID" not in cookie
    assert auth["code_challenge_method"] == "S256"

    with pytest.raises(HTTPException) as error:
        google.accept_google_callback(cookie, "google-code", "wrong-state")
    assert error.value.status_code == 401
    assert calls == ["auth", "login"]

    ticket = google.accept_google_callback(cookie, "google-code", "google-state")
    assert calls == ["auth", "login", "endpoint"]
    assert google.complete_google_sign_in(ticket) == {"access_token": "fixture"}


def test_google_rejects_callback_for_a_different_origin(monkeypatch):
    cfg = OidcConfig(ISSUER, "console", "fixture-secret", PRIVATE)
    monkeypatch.setattr(google, "require_oidc_config", lambda: cfg)

    def broker(request):
        if request.url.path.endswith("/auth"):
            location = ISSUER + "/broker/google/login"
        else:
            params = urlencode({"state": "a", "redirect_uri": "https://unrelated.invalid/return"})
            location = "https://accounts.google.com/o/oauth2/v2/auth?" + params
        return httpx.Response(303, headers={"Location": location})

    monkeypatch.setattr(google, "private_client", lambda: httpx.Client(
        transport=httpx.MockTransport(broker),
    ))
    with pytest.raises(HTTPException) as error:
        google.start_google_sign_in(CONSOLE)
    assert error.value.status_code == 502


def test_user_update_drops_only_the_legacy_password_action(monkeypatch):
    from pipeline.auth import keycloak_admin

    realm = "http://keycloak:8080/auth/admin/realms/kp"
    user = {
        "id": "existing-user", "username": "operator", "email": "operator@example.invalid",
        "requiredActions": ["UPDATE_PASSWORD", "VERIFY_EMAIL"],
    }
    updates = []

    def request(method, url, *, token, body=None):
        if method == "PUT" and url == f"{realm}/users/existing-user":
            updates.append(body)
            return 204, None
        if method == "GET" and url.startswith(realm + "/users?"):
            return 200, [user]
        if method == "GET" and url == f"{realm}/users/existing-user":
            return 200, user
        return 200, []

    monkeypatch.setattr(keycloak_admin, "require_admin_config", lambda: object())
    monkeypatch.setattr(keycloak_admin, "_admin_root", lambda _: realm)
    monkeypatch.setattr(keycloak_admin, "_admin_token", lambda _: "fixture")
    monkeypatch.setattr(keycloak_admin, "_req", request)
    monkeypatch.setattr(keycloak_admin, "ensure_access_group", lambda *a, **kw: ("/global/super-admin", {"id": "group"}))

    result = keycloak_admin.provision_user(email=user["email"], access_type="super_admin")
    assert result["user_id"] == user["id"]
    assert updates[0]["requiredActions"] == ["VERIFY_EMAIL"]
    assert "credentials" not in updates[0]
