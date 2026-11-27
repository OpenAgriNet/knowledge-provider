"""Use the private Keycloak email flow to issue the console's normal session."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from pipeline import db

from .oidc import OidcConfig, exchange_authorization_code, require_oidc_config
from .private_browser import private_client, private_realm, submit_private_form

CODE_TTL = 300
REDIRECT_DEFAULT = "http://127.0.0.1/knowledge-provider/auth/sso-callback"
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


class _LoginForm(HTMLParser):
    def __init__(self, html: str, field: str):
        super().__init__()
        self.field = field
        self.form_action = None
        self.action = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form" and attributes.get("method", "").lower() == "post":
            self.form_action = attributes.get("action")
        if tag == "input" and attributes.get("name") == self.field:
            if "disabled" not in attributes:
                self.action = self.form_action

    def handle_endtag(self, tag):
        if tag == "form":
            self.form_action = None


def _key(config: OidcConfig, purpose: bytes) -> bytes:
    return hmac.digest(config.client_secret.encode(), purpose, "sha256")


def _cipher(config: OidcConfig) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_key(config, b"kp-email-sign-in-challenge-v1")))


def _throttle(config: OidcConfig, email: str) -> None:
    address = hmac.digest(_key(config, b"kp-email-sign-in-throttle-v1"), email.encode(), "sha256")
    with db.get_connection() as connection:
        connection.execute("DELETE FROM email_sign_in_limits WHERE available_at <= now()")
        inserted = connection.execute(
            """
            INSERT INTO email_sign_in_limits (address_digest, available_at)
            VALUES (%s, now() + interval '60 seconds')
            ON CONFLICT (address_digest) DO UPDATE SET available_at = EXCLUDED.available_at
            WHERE email_sign_in_limits.available_at <= now()
            RETURNING 1
            """,
            (address,),
        ).fetchone()
    if not inserted:
        raise HTTPException(429, "Wait a minute before requesting another code.", headers={"Retry-After": "60"})


def _form_action(config: OidcConfig, response: httpx.Response, field: str) -> str | None:
    if response.status_code != 200:
        return None
    action = _LoginForm(response.text, field).action
    if not action:
        return None
    target = urlsplit(urljoin(str(response.request.url), action))
    private = urlsplit(private_realm(config))
    public = urlsplit(config.issuer)
    origins = {(item.scheme, item.netloc) for item in (private, public)}
    if (target.scheme, target.netloc) not in origins:
        raise HTTPException(502, "Sign-in is temporarily unavailable.")
    if target.path != f"{private.path}/login-actions/authenticate":
        raise HTTPException(502, "Sign-in is temporarily unavailable.")
    return f"{private.scheme}://{private.netloc}{target.path}?{target.query}"


def _sealed_challenge(config: OidcConfig, context: dict, *, issued_at: int | None = None) -> str:
    # Unknown and disabled accounts get the same response shape and size.
    context.pop("padding", None)
    context["padding"] = " " * max(0, 4096 - len(json.dumps(context, separators=(",", ":"))))
    payload = json.dumps(context, separators=(",", ":")).encode()
    cipher = _cipher(config)
    sealed = cipher.encrypt(payload) if issued_at is None else cipher.encrypt_at_time(payload, issued_at)
    return sealed.decode()


def send_email_code(raw_email: str) -> str:
    email = raw_email.strip().lower()
    if len(email) > 254 or not _EMAIL.fullmatch(email):
        raise HTTPException(422, "Enter a valid email address.")
    config = require_oidc_config()
    _throttle(config, email)
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    redirect = os.environ.get("KEYCLOAK_CONSOLE_REDIRECT_URI") or REDIRECT_DEFAULT
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    try:
        with private_client() as client:
            login = client.get(f"{private_realm(config)}/protocol/openid-connect/auth", params={
                "client_id": config.client_id, "response_type": "code", "scope": "openid profile email",
                "redirect_uri": redirect, "state": state, "code_challenge": challenge,
                "code_challenge_method": "S256", "prompt": "login", "kc_idp_hint": "",
            })
            username_action = _form_action(config, login, "username")
            if not username_action:
                raise HTTPException(503, "Email sign-in is temporarily unavailable.")
            code_page = submit_private_form(client, username_action, {"username": email})
            if code_page.status_code >= 500:
                raise HTTPException(503, "Email sign-in is temporarily unavailable.")
            code_action = _form_action(config, code_page, "emailCode")
            context = {
                "action": code_action, "cookies": dict(client.cookies.items()) if code_action else {},
                "verifier": verifier, "state": state, "redirect": redirect,
            }
    except httpx.HTTPError:
        raise HTTPException(503, "Email sign-in is temporarily unavailable.") from None

    return _sealed_challenge(config, context)


def verify_email_code(challenge: str, code: str) -> tuple[dict | None, str]:
    invalid = HTTPException(401, "That code is invalid or expired.")
    if not re.fullmatch(r"[0-9]{6}", code) or not 1 <= len(challenge) <= 12000:
        raise invalid
    config = require_oidc_config()
    cipher = _cipher(config)
    try:
        context = json.loads(cipher.decrypt(challenge.encode(), ttl=CODE_TTL))
    except InvalidToken:
        raise invalid from None
    if not context["action"]:
        return None, challenge

    try:
        with private_client(context["cookies"]) as client:
            result = submit_private_form(client, context["action"], {"emailCode": code})
            if result.status_code == 200:
                context["action"] = _form_action(config, result, "emailCode")
                context["cookies"] = dict(client.cookies.items()) if context["action"] else {}
                return None, _sealed_challenge(config, context, issued_at=cipher.extract_timestamp(challenge.encode()))
    except httpx.HTTPError:
        raise HTTPException(503, "Email sign-in is temporarily unavailable.") from None
    if result.status_code >= 500:
        raise HTTPException(503, "Email sign-in is temporarily unavailable.")
    if not result.is_redirect:
        raise invalid
    target = urlsplit(result.headers.get("Location", ""))
    callback = urlsplit(context["redirect"])
    if (target.scheme, target.netloc, target.path) != (callback.scheme, callback.netloc, callback.path):
        raise invalid
    query = parse_qs(target.query)
    if query.get("state") != [context["state"]] or len(query.get("code", [])) != 1:
        raise invalid
    return exchange_authorization_code(query["code"][0], context["redirect"], context["verifier"]), ""
