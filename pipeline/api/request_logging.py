from __future__ import annotations

import inspect
import logging
import time
from http import HTTPStatus
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("pipeline.api.requests")

QUIET_PATHS = frozenset({"/health"})


def install(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, recording(http_exception_handler))
    app.add_exception_handler(RequestValidationError, recording(request_validation_exception_handler))
    app.add_middleware(RequestLogging)


def recording(handler: Callable) -> Callable:
    async def record_then_handle(request: Request, exc: Exception):
        request.state.error = _detail(exc)
        request.state.error_cause = exc.__cause__
        result = handler(request, exc)
        return await result if inspect.isawaitable(result) else result

    return record_then_handle


def _detail(exc: Exception) -> Any:
    if isinstance(exc, RequestValidationError):
        return [{"type": e.get("type"), "loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]
    return getattr(exc, "detail", None)


class RequestLogging:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status: dict[str, int] = {}

        async def send_and_watch(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_watch)
        except Exception as exc:
            logger.error(
                "request failed",
                extra={
                    **_request_fields(scope, status.get("code", 500), _elapsed_ms(started)),
                    "error": repr(exc),
                },
                exc_info=True,
            )
            raise

        _log_response(scope, status.get("code", 500), _elapsed_ms(started))


def _log_response(scope: Scope, code: int, duration_ms: int) -> None:
    fields = _request_fields(scope, code, duration_ms)
    if code < 400:
        level = logging.DEBUG if scope["path"] in QUIET_PATHS else logging.INFO
        logger.log(level, "request completed", extra=fields)
        return

    state = scope.get("state") or {}
    error = state.get("error") or _reason(code)
    cause = state.get("error_cause")
    logger.log(
        logging.ERROR if code >= 500 else logging.WARNING,
        "request failed",
        extra={**fields, "error": str(error)},
        exc_info=(type(cause), cause, cause.__traceback__) if code >= 500 and cause else None,
    )


def _request_fields(scope: Scope, code: int, duration_ms: int) -> dict[str, Any]:
    return {
        "method": scope["method"],
        "uri": _uri(scope),
        "path": scope["path"],
        "status": code,
        "duration_ms": duration_ms,
    }


def _uri(scope: Scope) -> str:
    query = scope.get("query_string", b"").decode("latin-1")
    return f"{scope['path']}?{query}" if query else scope["path"]


def _reason(code: int) -> str:
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return str(code)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
