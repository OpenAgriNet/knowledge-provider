"""Unit tests for the passwordless email-OTP login helpers.

Keycloak itself is stubbed at the HTTP boundary (``_post``) — these cover the
parts we own: config resolution, email normalisation, and the translation of
Keycloak's error vocabulary into messages a signed-out user can act on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from pipeline.auth import email_otp

ENV = {
    "KEYCLOAK_ISSUER": "https://kc.example/auth/realms/test",
    "KEYCLOAK_CLIENT_ID": "test-client",
    "KEYCLOAK_CLIENT_SECRET": "s3cret",
}


def _post_returns(status, body):
    return patch.object(email_otp, "_post", return_value=(status, body))


def test_config_falls_back_to_vite_client_id():
    env = {
        "KEYCLOAK_ISSUER": "https://kc.example/auth/realms/test/",
        "VITE_KEYCLOAK_CLIENT_ID": "bharat-vistaar",
        "KEYCLOAK_CLIENT_SECRET": "s3cret",
    }
    with patch.dict("os.environ", env, clear=True):
        cfg = email_otp.load_email_otp_config()
    assert cfg.client_id == "bharat-vistaar"
    # Trailing slash must not survive into the derived URL.
    assert cfg.token_url == "https://kc.example/auth/realms/test/protocol/openid-connect/token"
    assert cfg.configured


def test_missing_secret_names_the_variable():
    with patch.dict("os.environ", {"KEYCLOAK_ISSUER": "https://kc.example/auth/realms/t"}, clear=True):
        with pytest.raises(HTTPException) as exc:
            email_otp.require_email_otp_config()
    assert exc.value.status_code == 503
    assert "KEYCLOAK_CLIENT_SECRET" in exc.value.detail


@pytest.mark.parametrize("raw", ["  User@Example.COM ", "user@example.com"])
def test_normalize_email_lowercases_and_trims(raw):
    assert email_otp.normalize_email(raw) == "user@example.com"


@pytest.mark.parametrize("raw", ["", "   ", "not-an-email"])
def test_normalize_email_rejects_junk(raw):
    with pytest.raises(HTTPException) as exc:
        email_otp.normalize_email(raw)
    assert exc.value.status_code == 400


def test_send_otp_emails_a_code_to_an_existing_account():
    with (
        patch.dict("os.environ", ENV, clear=True),
        patch.object(email_otp.db, "get_email_otp", return_value=None),
        patch.object(email_otp.db, "store_email_otp") as store_otp,
        patch.object(
            email_otp, "_find_keycloak_user", return_value={"id": "u1", "email": "user@example.com"}
        ),
        patch.object(email_otp, "_send_email") as send_email,
    ):
        result = email_otp.send_otp("user@example.com")

    # Enumeration-safe: the response never varies with account existence, so
    # it can't leak Keycloak/SMTP details either.
    assert result == {
        "status": "ok",
        "message": "If the account exists, a one-time code has been emailed to it",
        "resend_after_seconds": email_otp.OTP_RESEND_COOLDOWN_SECONDS,
    }
    store_otp.assert_called_once()
    send_email.assert_called_once()
    assert send_email.call_args.args[0] == "user@example.com"


def test_send_otp_swallows_smtp_failures_for_enumeration_safety():
    # A broken SMTP relay must not produce a different response/status than
    # the "no such account" path, or the difference becomes an oracle.
    with (
        patch.dict("os.environ", ENV, clear=True),
        patch.object(email_otp.db, "get_email_otp", return_value=None),
        patch.object(email_otp.db, "store_email_otp"),
        patch.object(
            email_otp, "_find_keycloak_user", return_value={"id": "u1", "email": "user@example.com"}
        ),
        patch.object(email_otp, "_send_email", side_effect=HTTPException(503, "smtp down")),
    ):
        result = email_otp.send_otp("user@example.com")

    assert result == {
        "status": "ok",
        "message": "If the account exists, a one-time code has been emailed to it",
        "resend_after_seconds": email_otp.OTP_RESEND_COOLDOWN_SECONDS,
    }


def test_verify_returns_the_full_token_set():
    email = "user@example.com"
    code = "123456"
    minted = {
        "access_token": "at",
        "refresh_token": "rt",
        "id_token": "it",
        "token_type": "Bearer",
        "expires_in": 300,
        "refresh_expires_in": 1800,
    }
    with patch.dict("os.environ", ENV, clear=True):
        row = {
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "attempts": 0,
            "code_hash": email_otp._hash_code(email, code),
        }
        with (
            patch.object(email_otp.db, "get_email_otp", return_value=row),
            patch.object(email_otp.db, "delete_email_otp") as delete_otp,
            patch.object(email_otp, "_mint_tokens_for_verified_email", return_value=minted),
        ):
            tokens = email_otp.verify_otp(email, code)

    delete_otp.assert_called_once_with(email)
    assert tokens["access_token"] == "at"
    assert tokens["refresh_token"] == "rt"
    assert tokens["id_token"] == "it"


def _otp_row(email, code, **overrides):
    row = {
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "attempts": 0,
        "code_hash": email_otp._hash_code(email, code),
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "row,expected",
    [
        (None, "no longer valid"),
        ("expired", "expired"),
        ("too_many_attempts", "Too many"),
        ("wrong_code", "incorrect"),
    ],
    ids=["no-live-code", "expired", "too-many-attempts", "wrong-code"],
)
def test_verify_maps_failure_states_to_actionable_text(row, expected):
    email, code = "user@example.com", "123456"
    with patch.dict("os.environ", ENV, clear=True):
        if row == "expired":
            row = _otp_row(
                email, code, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            )
        elif row == "too_many_attempts":
            row = _otp_row(email, code, attempts=email_otp.OTP_MAX_ATTEMPTS)
        elif row == "wrong_code":
            row = _otp_row(email, code, code_hash="not-the-real-hash")

        with (
            patch.object(email_otp.db, "get_email_otp", return_value=row),
            patch.object(email_otp.db, "delete_email_otp"),
            patch.object(email_otp.db, "increment_email_otp_attempts"),
        ):
            with pytest.raises(HTTPException) as exc:
                email_otp.verify_otp(email, code)
    assert exc.value.status_code == 401
    assert expected in exc.value.detail


def test_verify_flags_a_missing_flow_binding_as_misconfiguration():
    # What Keycloak says when the client has no Direct Access Grants enabled,
    # surfaced during the post-verification password-grant bridge.
    email, code = "user@example.com", "123456"
    with patch.dict("os.environ", ENV, clear=True):
        row = _otp_row(email, code)
        with (
            patch.object(email_otp.db, "get_email_otp", return_value=row),
            patch.object(email_otp.db, "delete_email_otp"),
            patch.object(
                email_otp, "_find_keycloak_user", return_value={"id": "u1", "email": email}
            ),
            patch.object(email_otp.keycloak_admin, "require_admin_config", return_value=object()),
            patch.object(email_otp.keycloak_admin, "_admin_token", return_value="admin-tok"),
            patch.object(email_otp.keycloak_admin, "_admin_root", return_value="https://kc.example/admin"),
            patch.object(email_otp.keycloak_admin, "_req", return_value=(204, None)),
            _post_returns(400, {"error": "unauthorized_client", "error_description": "Client not allowed"}),
        ):
            with pytest.raises(HTTPException) as exc:
                email_otp.verify_otp(email, code)
    assert exc.value.status_code == 503
    assert "Direct Access Grants" in exc.value.detail


def test_verify_requires_a_code():
    with patch.dict("os.environ", ENV, clear=True):
        with pytest.raises(HTTPException) as exc:
            email_otp.verify_otp("user@example.com", "   ")
    assert exc.value.status_code == 400


def test_refresh_reports_a_dead_session_as_401():
    with patch.dict("os.environ", ENV, clear=True), _post_returns(
        400, {"error": "invalid_grant", "error_description": "Token is not active"}
    ):
        with pytest.raises(HTTPException) as exc:
            email_otp.refresh_tokens("stale")
    assert exc.value.status_code == 401
