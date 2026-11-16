"""HTTP used by Python to complete a private Keycloak browser session."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

from .oidc import OidcConfig

_private_http = ContextVar("private_sign_in_http", default=False)


def _log_public_http(_record: logging.LogRecord) -> bool:
    return not _private_http.get()


# Keep authentication session URLs and cookies out of HTTP request logs.
for _logger in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.proxy"):
    logging.getLogger(_logger).addFilter(_log_public_http)


@contextmanager
def private_client(cookies: dict[str, str] | None = None):
    marker = _private_http.set(True)
    try:
        with httpx.Client(cookies=cookies, timeout=10, trust_env=False, follow_redirects=False) as client:
            yield client
    finally:
        _private_http.reset(marker)


def private_cookie_header(client: httpx.Client) -> dict[str, str]:
    # Actions are resolved to the configured private realm. Forward its session
    # cookies explicitly when TLS terminates before the private Keycloak hop.
    cookie = "; ".join(f"{item.name}={item.value}" for item in client.cookies.jar if not item.is_expired())
    return {"Cookie": cookie}


def submit_private_form(client: httpx.Client, action: str, fields: dict[str, str]) -> httpx.Response:
    return client.post(action, data=fields, headers=private_cookie_header(client))


def private_realm(config: OidcConfig) -> str:
    return config.backchannel_url or config.issuer
