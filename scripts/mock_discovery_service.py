#!/usr/bin/env python3
"""Mock stand-in for the external Discovery Service (local pipeline unblocking).

Implements the surface `pipeline/discovery_publish_service.py` calls:
  POST /publish

Every inbound request (except GET /_requests itself) is recorded in memory
with its time, method, path, headers, and body, viewable via:
  GET /_requests

No Docker, no persistence - state resets on restart.

Usage:
  python scripts/mock_discovery_service.py
  # binds to a random OS-assigned port by default; override with:
  MOCK_DISCOVERY_SERVICE_PORT=9100 python scripts/mock_discovery_service.py

Point the pipeline at it:
  DISCOVERY_SERVICE_ENDPOINT=http://localhost:<port>
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock Discovery Service", version="1.0.0")

recorded_requests: list[dict] = []


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
async def publish() -> JSONResponse:
    return JSONResponse(content={"message": {"ack": {"status": "ACK"}}})


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
        "(publish: /publish, requests: /_requests)"
    )
    uvicorn.run(app, host=host, port=port)
