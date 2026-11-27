import asyncio
import os

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, SecretStr

from pipeline.api.rate_limiting import limiter
from pipeline.auth.config import load_auth_config
from pipeline.auth.deps import CurrentUser
from pipeline.auth.email_sign_in import send_email_code, verify_email_code
from pipeline.auth.google_sign_in import (
    GOOGLE_RESULT_COOKIE,
    GOOGLE_RESULT_TTL,
    GOOGLE_STATE_COOKIE,
    GOOGLE_STATE_TTL,
    accept_google_callback,
    complete_google_sign_in,
    google_console,
    require_google_console,
    start_google_sign_in,
)
from pipeline.auth.oidc import end_session, refresh_tokens
from pipeline.auth.tenancy import portal_instance

RATE_LIMIT_DEFAULT = os.environ.get("RATE_LIMIT_DEFAULT", "100/minute")
RATE_LIMIT_SIGN_IN = os.environ.get("RATE_LIMIT_SIGN_IN", os.environ.get("RATE_LIMIT_OTP", "10/minute"))

router = APIRouter()


class SessionRefreshBody(BaseModel):
    refresh_token: str = Field(..., description="Refresh token from a Keycloak session")


class EmailCodeRequest(BaseModel):
    email: SecretStr


class EmailCodeVerification(BaseModel):
    challenge: SecretStr
    code: SecretStr


@router.post("/auth/email/send", status_code=202)
@limiter.limit(RATE_LIMIT_SIGN_IN)
async def auth_send_email(request: Request, response: Response, body: EmailCodeRequest):
    challenge = await asyncio.to_thread(send_email_code, body.email.get_secret_value())
    response.headers["Cache-Control"] = "no-store"
    return {"challenge": challenge}


@router.post("/auth/email/verify")
@limiter.limit(RATE_LIMIT_SIGN_IN)
async def auth_verify_email(request: Request, body: EmailCodeVerification):
    tokens, challenge = await asyncio.to_thread(
        verify_email_code, body.challenge.get_secret_value(), body.code.get_secret_value(),
    )
    headers = {"Cache-Control": "no-store"}
    if tokens is None:
        return JSONResponse(
            {"detail": "That code is invalid or expired.", "challenge": challenge},
            status_code=401, headers=headers,
        )
    return JSONResponse(tokens, headers=headers)


@router.get("/auth/google")
async def google_status():
    return {"enabled": google_console() is not None}


@router.post("/auth/google/start")
@limiter.limit(RATE_LIMIT_SIGN_IN)
async def google_start(request: Request, response: Response):
    console = require_google_console()
    location, state = await asyncio.to_thread(start_google_sign_in, console)
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(GOOGLE_RESULT_COOKIE)
    response.set_cookie(GOOGLE_STATE_COOKIE, state, max_age=GOOGLE_STATE_TTL, secure=console.secure, httponly=True)
    return {"location": location}


@router.get("/auth/google/callback", include_in_schema=False)
@limiter.limit(RATE_LIMIT_SIGN_IN)
async def google_callback(request: Request):
    console = require_google_console()
    response = RedirectResponse(f"{console.login_path}#google-error", status_code=303, headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    })
    if request.query_params.get("error"):
        return response
    codes = request.query_params.getlist("code")
    states = request.query_params.getlist("state")
    if len(codes) != 1 or len(states) != 1:
        return response
    try:
        result = await asyncio.to_thread(
            accept_google_callback, request.cookies.get(GOOGLE_STATE_COOKIE), codes[0], states[0],
        )
    except HTTPException:
        return response
    response.delete_cookie(GOOGLE_STATE_COOKIE)
    response.headers["Location"] = f"{console.login_path}#google-complete"
    response.set_cookie(GOOGLE_RESULT_COOKIE, result, max_age=GOOGLE_RESULT_TTL, secure=console.secure, httponly=True)
    return response


@router.post("/auth/google/complete")
@limiter.limit(RATE_LIMIT_SIGN_IN)
async def google_complete(request: Request):
    require_google_console()
    try:
        tokens = await asyncio.to_thread(complete_google_sign_in, request.cookies.get(GOOGLE_RESULT_COOKIE))
    except HTTPException:
        response = JSONResponse({"detail": "Google sign-in could not be completed."}, status_code=401)
    else:
        response = JSONResponse(tokens)
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(GOOGLE_RESULT_COOKIE)
    return response


@router.post("/auth/session/logout", status_code=204)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def auth_session_logout(request: Request, body: SessionRefreshBody):
    await asyncio.to_thread(end_session, body.refresh_token)
    return Response(status_code=204)


@router.post("/auth/session/refresh")
@router.post("/auth/otp/refresh")  # pre-SSO-exchange name; kept so older UI builds keep working
@limiter.limit(RATE_LIMIT_DEFAULT)
async def auth_session_refresh(request: Request, response: Response, body: SessionRefreshBody):
    """Renew a session. The refresh token authenticates the request."""
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(refresh_tokens, body.refresh_token)


@router.get("/auth/me")
async def auth_me(user: CurrentUser):
    """Return the authenticated caller (local bypass user when AUTH_DISABLED=true).

    Identity fields (username/email/roles) come from the JWT when a Bearer token
    is present, including in AUTH_DISABLED mode for UI display.
    """
    display_name = (user.username or "").strip()
    return {
        "user_id": user.user_id,
        "username": user.username,
        "name": display_name,
        "email": user.email,
        "roles": user.roles,
        "permissions": sorted(p.value for p in user.permissions),
        "instances": user.instances,
        "envs": user.envs,
        # Keycloak group paths (/states/MH/contributor, /global/super-admin)
        # and derived per-state role map for tenant-aware UI.
        "groups": list(user.groups or []),
        "state_roles": dict(user.state_roles or {}),
        "is_super_admin": bool(user.is_superadmin),
        "is_global_viewer": user.is_global_viewer,
        "is_bh_viewer": user.is_global_viewer,  # compatibility for older consoles
        "live_provider_access": not user.token_disabled_mode and load_auth_config().verify_live_groups,
        # Portal instance code for BV / platform docs (not a state).
        "portal_instance": portal_instance(),
        "auth_disabled": user.token_disabled_mode,
    }
