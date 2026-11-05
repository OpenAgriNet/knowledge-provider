from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("pipeline.api.requests")


def install(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _record_http_error)
    app.add_exception_handler(RequestValidationError, _record_validation_error)
    app.middleware("http")(_log_request)


async def _record_http_error(request: Request, exc: StarletteHTTPException):
    request.state.error = exc.detail
    return await http_exception_handler(request, exc)


async def _record_validation_error(request: Request, exc: RequestValidationError):
    request.state.error = [
        {"type": e.get("type"), "loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()
    ]
    return await request_validation_exception_handler(request, exc)


def _uri(request: Request) -> str:
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


async def _log_request(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.exception(
            "request method=%s uri=%s status=500 duration_ms=%d error=%r",
            request.method,
            _uri(request),
            _elapsed_ms(started),
            exc,
        )
        raise

    status = response.status_code
    if status < 400:
        logger.info(
            "request method=%s uri=%s status=%s duration_ms=%d",
            request.method,
            _uri(request),
            status,
            _elapsed_ms(started),
        )
    else:
        logger.warning(
            "request method=%s uri=%s status=%s duration_ms=%d error=%r",
            request.method,
            _uri(request),
            status,
            _elapsed_ms(started),
            getattr(request.state, "error", None),
        )
    return response


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
