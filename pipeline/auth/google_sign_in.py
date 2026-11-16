"""Broker Google through Keycloak while keeping the Keycloak session private."""

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from .email_sign_in import REDIRECT_DEFAULT
from .oidc import OidcConfig, exchange_authorization_code, require_oidc_config
from .private_browser import private_client, private_cookie_header, private_realm

GOOGLE_STATE_TTL = 600
GOOGLE_RESULT_TTL = 60
GOOGLE_STATE_COOKIE = "kp_google_state"
GOOGLE_RESULT_COOKIE = "kp_google_result"


@dataclass(frozen=True)
class GoogleConsole:
    url: str

    @property
    def secure(self) -> bool:
        return urlsplit(self.url).scheme == "https"

    @property
    def login_path(self) -> str:
        return urlsplit(self.url).path


def google_console() -> GoogleConsole | None:
    value = os.environ.get("KEYCLOAK_GOOGLE_CONSOLE_URL", "").strip()
    if not value:
        return None
    url = urlsplit(value)
    loopback = url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}
    if (not (url.scheme == "https" or loopback) or not url.netloc or url.username is not None
            or not url.path.startswith("/") or url.path.startswith("//") or url.query or url.fragment):
        raise HTTPException(503, "Google sign-in is temporarily unavailable.")
    return GoogleConsole(value)


def require_google_console() -> GoogleConsole:
    console = google_console()
    if console is None:
        raise HTTPException(404, "Google sign-in is unavailable.")
    return console


def _cipher(config: OidcConfig, purpose: str) -> Fernet:
    key = hmac.digest(config.client_secret.encode(), f"kp-google-{purpose}-v1".encode(), "sha256")
    return Fernet(base64.urlsafe_b64encode(key))


def _seal(config: OidcConfig, purpose: str, data: dict) -> str:
    return _cipher(config, purpose).encrypt(json.dumps(data, separators=(",", ":")).encode()).decode()


def _open(config: OidcConfig, purpose: str, data: str | None, ttl: int) -> dict:
    if data is None or len(data) > 4000:
        raise HTTPException(401, "Start Google sign-in again.")
    try:
        return json.loads(_cipher(config, purpose).decrypt(data.encode(), ttl=ttl))
    except InvalidToken:
        raise HTTPException(401, "Start Google sign-in again.") from None


def _redirect(response: httpx.Response) -> str:
    if not response.is_redirect or not response.headers.get("Location"):
        raise HTTPException(502, "Google sign-in is temporarily unavailable.")
    return urljoin(str(response.request.url), response.headers["Location"])


def _private_redirect(config: OidcConfig, location: str, suffix: str) -> str:
    target = urlsplit(location)
    private = urlsplit(private_realm(config))
    realms = (urlsplit(config.issuer), private)
    if not any(
        (target.scheme, target.netloc, target.path) == (realm.scheme, realm.netloc, realm.path + suffix)
        for realm in realms
    ):
        raise HTTPException(502, "Google sign-in is temporarily unavailable.")
    return f"{private_realm(config)}{suffix}?{target.query}"


def _console_code(location: str, context: dict) -> str:
    target = urlsplit(location)
    callback = urlsplit(context["redirect"])
    if (target.scheme, target.netloc, target.path) != (callback.scheme, callback.netloc, callback.path):
        raise HTTPException(401, "Google sign-in requires a linked account. Use email to sign in.")
    params = parse_qs(target.query)
    if params.get("state") != [context["state"]] or len(params.get("code", ())) != 1:
        raise HTTPException(401, "Google sign-in could not be completed.")
    return params["code"][0]


def start_google_sign_in(console: GoogleConsole) -> tuple[str, str]:
    config = require_oidc_config()
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    redirect = os.environ.get("KEYCLOAK_CONSOLE_REDIRECT_URI") or REDIRECT_DEFAULT
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    try:
        with private_client() as client:
            start = client.get(f"{private_realm(config)}/protocol/openid-connect/auth", params={
                "client_id": config.client_id, "response_type": "code", "scope": "openid profile email",
                "redirect_uri": redirect, "state": state, "code_challenge": challenge,
                "code_challenge_method": "S256", "kc_idp_hint": "google", "prompt": "select_account",
            })
            broker = _private_redirect(config, _redirect(start), "/broker/google/login")
            google = _redirect(client.get(broker, headers=private_cookie_header(client)))
            cookies = dict(client.cookies.items())
    except httpx.HTTPError:
        raise HTTPException(502, "Google sign-in is temporarily unavailable.") from None

    target = urlsplit(google)
    query = parse_qs(target.query)
    issuer = urlsplit(config.issuer)
    origin = urlsplit(console.url)
    callback = f"{origin.scheme}://{origin.netloc}{issuer.path}/broker/google/endpoint"
    if ((target.scheme, target.netloc, target.path) != ("https", "accounts.google.com", "/o/oauth2/v2/auth")
            or query.get("redirect_uri") != [callback] or len(query.get("state", ())) != 1):
        raise HTTPException(502, "Google sign-in is temporarily unavailable.")
    return google, _seal(config, "state", {
        "state": state, "verifier": verifier, "redirect": redirect, "cookies": cookies,
        "google_state": query["state"][0],
    })


def accept_google_callback(cookie: str | None, code: str, state: str) -> str:
    config = require_oidc_config()
    context = _open(config, "state", cookie, GOOGLE_STATE_TTL)
    if not code or len(code) > 4096 or not state or len(state) > 4096:
        raise HTTPException(401, "Google sign-in could not be completed.")
    if not hmac.compare_digest(state.encode(), context["google_state"].encode()):
        raise HTTPException(401, "Start Google sign-in again.")
    try:
        with private_client(context["cookies"]) as client:
            result = client.get(f"{private_realm(config)}/broker/google/endpoint", params={
                "code": code, "state": state,
            })
    except httpx.HTTPError:
        raise HTTPException(502, "Google sign-in is temporarily unavailable.") from None
    console_code = _console_code(_redirect(result), context)
    return _seal(config, "result", {
        "code": console_code, "verifier": context["verifier"], "redirect": context["redirect"],
    })


def complete_google_sign_in(cookie: str | None) -> dict:
    context = _open(require_oidc_config(), "result", cookie, GOOGLE_RESULT_TTL)
    return exchange_authorization_code(context["code"], context["redirect"], context["verifier"])
