#!/usr/bin/env python3
"""One-shot Keycloak setup for the local docker compose stack.

Runs as the `keycloak-bootstrap` compose service once Keycloak is up. Idempotent.

  1. Waits for the realm to be served (Keycloak has no healthcheck).
  2. keycloak_bootstrap_tenant_groups.py — realm roles, /global/* and
     /states/{CODE}/* groups, and the `groups` claim mapper on the UI client.
  3. Adds the users named in BOOTSTRAP_SUPER_ADMIN_EMAILS to /global/super-admin
     and those in BOOTSTRAP_STATE_USERS ("email:STATE:role", role defaults to
     state_admin) to their state group.

Users sign in with email OTP (needs KC_SMTP_* in .env) or SSO. Only the Keycloak
account is created here — no password is set.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE = os.environ.get("KEYCLOAK_BASE_URL", "http://keycloak:8080/auth").rstrip("/")
REALM = os.environ.get("KEYCLOAK_REALM", "docs-pipeline")
WAIT_SECONDS = int(os.environ.get("KEYCLOAK_BOOTSTRAP_WAIT_SECONDS", "300"))


def wait_for_realm() -> None:
    url = f"{BASE}/realms/{REALM}/.well-known/openid-configuration"
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    print(f"keycloak realm={REALM} is up")
                    return
        except (urllib.error.URLError, OSError):
            pass
        if time.monotonic() > deadline:
            sys.exit(f"ERROR: {url} not reachable after {WAIT_SECONDS}s")
        time.sleep(3)


def _csv(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def main() -> int:
    wait_for_realm()

    password = os.environ.get("KEYCLOAK_ADMIN_PASSWORD", "")
    if not password:
        sys.exit("ERROR: KEYCLOAK_ADMIN_PASSWORD (KC_ADMIN_PASSWORD) is not set")

    groups = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "keycloak_bootstrap_tenant_groups.py"),
            "--base-url", BASE,
            "--realm", REALM,
            "--admin-user", os.environ.get("KEYCLOAK_ADMIN", "admin"),
            "--admin-password", password,
        ],
        check=False,
    )
    if groups.returncode:
        return groups.returncode

    from pipeline.auth.keycloak_admin import provision_user

    failed = 0
    for email in _csv("BOOTSTRAP_SUPER_ADMIN_EMAILS"):
        try:
            provision_user(email=email, access_type="super_admin")
            print(f"user=ok email={email} group=/global/super-admin")
        except Exception as exc:  # noqa: BLE001 — report and keep going
            failed += 1
            print(f"user=FAIL email={email}: {exc}", file=sys.stderr)

    for spec in _csv("BOOTSTRAP_STATE_USERS"):
        email, _, rest = spec.partition(":")
        state, _, role = rest.partition(":")
        try:
            provision_user(
                email=email, access_type="state",
                state=state.upper(), role=role or "state_admin",
            )
            print(f"user=ok email={email} group=/states/{state.upper()}/{role or 'state_admin'}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"user=FAIL spec={spec}: {exc}", file=sys.stderr)

    print("bootstrap_local_keycloak=ok" if not failed else f"bootstrap_local_keycloak=partial failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
