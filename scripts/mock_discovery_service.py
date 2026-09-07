#!/usr/bin/env python3
"""Mock stand-in for the external Discovery Service (local pipeline unblocking).

Implements the surface `pipeline/discovery_publish_service.py` calls:
  POST /publish

Validates the envelope well enough to catch the mistakes that matter - a
missing context, an empty `catalogs` array (the spec declares `minItems: 1`),
a catalog with no resources - and answers with the spec's `OnPublishResponse`
shape, one `CatalogProcessingResult` per submitted catalog. It is not a
conformance checker: catalogs and resources are carried as opaque JSON, exactly
as the real service documents.

Every inbound request (except GET /_requests itself) is recorded in memory
with its time, method, path, headers, and body, viewable via:
  GET /_requests

No Docker, no persistence - state resets on restart.

Usage:
  python scripts/mock_discovery_service.py
  # binds to a random OS-assigned port by default; override with:
  MOCK_DISCOVERY_SERVICE_PORT=9100 python scripts/mock_discovery_service.py

  # answer every catalog with REJECTED, to exercise the pipeline's failure path:
  MOCK_DISCOVERY_SERVICE_RESULT_STATUS=REJECTED python scripts/mock_discovery_service.py

Point the pipeline at it:
  DISCOVERY_SERVICE_ENDPOINT=http://localhost:<port>
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

app = FastAPI(title="Mock Discovery Service", version="1.0.0")

recorded_requests: list[dict] = []

RESULT_STATUS_ENV_VAR = "MOCK_DISCOVERY_SERVICE_RESULT_STATUS"
DEFAULT_RESULT_STATUS = "ACCEPTED"


class Catalog(BaseModel):
    """Permissive, like the real service: only `id` and the resources matter here."""

    id: str = Field(min_length=1)
    resources: list[dict[str, Any]] = Field(min_length=1)

    model_config = {"extra": "allow"}


class CatalogPublishAction(BaseModel):
    catalogs: list[Catalog] = Field(min_length=1)

    model_config = {"extra": "allow"}


class PublishRequest(BaseModel):
    context: dict[str, Any]
    message: CatalogPublishAction

    @field_validator("context")
    @classmethod
    def context_must_identify_the_sender(cls, value: dict) -> dict:
        for field in ("action", "senderId", "transactionId", "messageId"):
            if not value.get(field):
                raise ValueError(f"context.{field} is required")
        return value


def _configured_result_status() -> str:
    """Verdict this instance answers with. Read per request so it can be flipped."""
    return (os.environ.get(RESULT_STATUS_ENV_VAR) or DEFAULT_RESULT_STATUS).strip().upper()


def _errors_for(result_status: str) -> list[dict]:
    if result_status == "ACCEPTED":
        return []
    return [
        {
            "code": "SCH_SCHEMA_VALIDATION_FAILED",
            "message": f"forced {result_status} by {RESULT_STATUS_ENV_VAR}",
        }
    ]


@app.exception_handler(RequestValidationError)
async def invalid_envelope(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Answer a malformed envelope the way the real service does.

    FastAPI's native 422 would still trip `raise_for_status()`, but the real
    service returns 400 with a single `Error` naming the offending JSONPath,
    and a mock that reports a different status than production is a mock you
    end up debugging instead of using.
    """
    first = exc.errors()[0]
    path = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "SCH_SCHEMA_VALIDATION_FAILED",
                "message": first.get("msg", "invalid publish envelope"),
                "details": {"path": f"$.{path}" if path else "$"},
            }
        },
    )


@app.middleware("http")
async def record_request(request: Request, call_next):
    if request.url.path == "/_requests":
        return await call_next(request)

    body = await request.body()
    try:
        body_recorded = json.loads(body)
    except json.JSONDecodeError:
        try:
            body_recorded = body.decode("utf-8")
        except UnicodeDecodeError:
            body_recorded = repr(body)

    recorded_requests.append(
        {
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "method": request.method,
            "path": request.url.path,
            "headers": dict(request.headers),
            "body": body_recorded,
        }
    )
    return await call_next(request)


@app.post("/publish")
async def publish(payload: PublishRequest) -> JSONResponse:
    result_status = _configured_result_status()
    errors = _errors_for(result_status)

    # A 200 with per-catalog result statuses, not a bare ACK: the caller is
    # expected to read message.results[].status rather than the status code.
    return JSONResponse(
        content={
            "context": {
                **payload.context,
                "action": "catalog/on_publish",
                "messageId": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            },
            "message": {
                "results": [
                    {
                        "catalogId": catalog.id,
                        "status": result_status,
                        "stats": {
                            "itemCount": len(catalog.resources),
                            "providerCount": 1,
                            "categoryCount": 1,
                        },
                        "errors": errors,
                    }
                    for catalog in payload.message.catalogs
                ]
            },
        }
    )


@app.get("/_requests")
def get_requests() -> list[dict]:
    return recorded_requests


def _pick_port(host: str) -> int:
    env_port = os.environ.get("MOCK_DISCOVERY_SERVICE_PORT")
    if env_port:
        return int(env_port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


if __name__ == "__main__":
    host = os.environ.get("MOCK_DISCOVERY_SERVICE_HOST", "0.0.0.0")
    port = _pick_port(host)
    print(
        f"Mock Discovery Service listening on http://{host}:{port} "
        f"(publish: /publish, requests: /_requests, result: {_configured_result_status()})"
    )
    uvicorn.run(app, host=host, port=port)
