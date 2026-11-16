import time
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import HTTPException

from pipeline.auth import email_sign_in
from pipeline.auth.oidc import OidcConfig

PRIVATE = "http://keycloak:8080/auth/realms/knowledge"
ISSUER = "https://identity.example.invalid/auth/realms/knowledge"


@pytest.fixture
def oidc(monkeypatch):
    cfg = OidcConfig(ISSUER, "console", "test-secret", PRIVATE)
    monkeypatch.setattr(email_sign_in, "require_oidc_config", lambda: cfg)
    return cfg


def test_email_sign_in_keeps_pkce_private_and_accepts_correct_code_after_retry(monkeypatch, oidc):
    state = {}
    calls = []

    def render(field, step):
        return httpx.Response(200, text=(
            f'<form method="post" action="{ISSUER}/login-actions/authenticate?step={step}">'
            f'<input name="{field}"></form>'
        ))

    def serve(request):
        assert str(request.url).startswith(PRIVATE)
        calls.append(request.method)
        if request.method == "GET":
            state.update(request.url.params)
            assert state["code_challenge_method"] == "S256"
            response = render("username", "email")
            response.headers["Set-Cookie"] = "AUTH_SESSION_ID=fixture; Path=/auth; HttpOnly; Secure"
            return response
        assert request.headers.get("Cookie") == "AUTH_SESSION_ID=fixture"
        if request.url.params["step"] == "email":
            assert parse_qs(request.content.decode()) == {"username": ["person@example.invalid"]}
            return render("emailCode", "code")
        if request.url.params["step"] == "code":
            assert parse_qs(request.content.decode()) == {"emailCode": ["000000"]}
            return render("emailCode", "retry")
        return httpx.Response(302, headers={
            "Location": f'{state["redirect_uri"]}?state={state["state"]}&code=private-code',
        })

    def make_client(cookies=None):
        return httpx.Client(cookies=cookies, transport=httpx.MockTransport(serve))

    def exchange(code, redirect, verifier):
        assert code == "private-code"
        assert redirect == state["redirect_uri"]
        assert len(verifier) >= 43
        return {"access_token": "fixture"}

    monkeypatch.setattr(email_sign_in, "private_client", make_client)
    monkeypatch.setattr(email_sign_in, "_throttle", lambda cfg, address: None)
    monkeypatch.setattr(email_sign_in, "exchange_authorization_code", exchange)
    challenge = email_sign_in.send_email_code("PERSON@example.invalid")
    assert PRIVATE not in challenge and state["state"] not in challenge
    tokens, retry = email_sign_in.verify_email_code(challenge, "000000")
    assert tokens is None and challenge != retry
    cipher = email_sign_in._cipher(oidc)
    assert cipher.extract_timestamp(challenge.encode()) == cipher.extract_timestamp(retry.encode())
    assert email_sign_in.verify_email_code(retry, "123456")[0] == {"access_token": "fixture"}
    assert calls == ["GET", "POST", "POST", "POST"]


def test_expired_challenge_never_reaches_keycloak(monkeypatch, oidc):
    challenge = email_sign_in._sealed_challenge(
        oidc, {"action": "never called"}, issued_at=int(time.time()) - email_sign_in.CODE_TTL - 1,
    )
    monkeypatch.setattr(email_sign_in, "private_client", lambda *_: pytest.fail("expired challenge reached Keycloak"))
    with pytest.raises(HTTPException) as error:
        email_sign_in.verify_email_code(challenge, "123456")
    assert error.value.status_code == 401


@pytest.mark.db
def test_send_throttle_applies_across_requests(db_connection, oidc):
    email = "same-address@example.invalid"
    email_sign_in._throttle(oidc, email)
    with pytest.raises(HTTPException) as error:
        email_sign_in._throttle(oidc, email)
    assert error.value.status_code == 429
