import logging

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from pipeline.api import request_logging

LOGGER = "pipeline.api.requests"


class Upload(BaseModel):
    pages: int


@pytest.fixture
def client():
    app = FastAPI()
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, request_logging.recording(_rate_limit_exceeded_handler))
    request_logging.install(app)

    @app.get("/ok")
    def ok():
        return {"secret": "body"}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/missing")
    def missing():
        raise HTTPException(404, "Document not found")

    @app.get("/boom")
    def boom():
        raise RuntimeError("database locked")

    @app.get("/wrapped")
    def wrapped():
        try:
            raise ConnectionError("qdrant unreachable")
        except ConnectionError as exc:
            raise HTTPException(503, "Search backend unavailable") from exc

    @app.post("/upload")
    def upload(body: Upload):
        return body

    @app.get("/limited")
    @limiter.limit("1/minute")
    def limited(request: Request):
        return {"ok": True}

    @app.get("/stream")
    def stream():
        def chunks():
            yield b"first"
            raise RuntimeError("minio dropped")

        return StreamingResponse(chunks())

    return TestClient(app, raise_server_exceptions=False)


def _lines(caplog):
    return [r for r in caplog.records if r.name == LOGGER]


def test_success_logs_method_uri_and_status_without_body(client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        client.get("/ok?index=documents")

    [record] = _lines(caplog)
    assert record.levelno == logging.INFO
    assert (record.method, record.uri, record.status) == ("GET", "/ok?index=documents", 200)
    assert record.path == "/ok"
    assert isinstance(record.duration_ms, int)
    assert "secret" not in str(vars(record))


def test_successful_health_checks_stay_at_debug(client, caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        client.get("/health")

    [record] = _lines(caplog)
    assert record.levelno == logging.DEBUG


def test_client_error_logs_warning_with_detail(client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        response = client.get("/missing")

    assert response.json() == {"detail": "Document not found"}
    [record] = _lines(caplog)
    assert record.levelno == logging.WARNING
    assert (record.uri, record.status) == ("/missing", 404)
    assert record.error == "Document not found"


def test_validation_error_logs_where_and_why_but_not_the_submitted_value(client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        response = client.post("/upload", json={"pages": "farmer-typed-value"})

    assert response.status_code == 422
    [record] = _lines(caplog)
    assert record.status == 422
    assert "int_parsing" in record.error and "pages" in record.error
    assert "farmer-typed-value" not in str(vars(record))


def test_unknown_route_logs_404(client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        client.get("/nowhere")

    [record] = _lines(caplog)
    assert (record.uri, record.status) == ("/nowhere", 404)
    assert record.error == "Not Found"


def test_rate_limited_request_logs_429_with_its_limit(client, caplog):
    client.get("/limited")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        response = client.get("/limited")

    assert response.status_code == 429
    [record] = _lines(caplog)
    assert record.status == 429
    assert "1 per 1 minute" in record.error


def test_server_error_from_a_caught_exception_keeps_its_cause(client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        response = client.get("/wrapped")

    assert response.status_code == 503
    [record] = _lines(caplog)
    assert record.levelno == logging.ERROR
    assert record.error == "Search backend unavailable"
    assert isinstance(record.exc_info[1], ConnectionError)


def test_unhandled_exception_logs_500_with_traceback(client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        response = client.get("/boom")

    assert response.status_code == 500
    [record] = _lines(caplog)
    assert record.levelno == logging.ERROR
    assert (record.uri, record.status) == ("/boom", 500)
    assert record.error == "RuntimeError('database locked')"
    assert record.exc_info is not None


def test_failure_mid_stream_is_logged(client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        try:
            client.get("/stream")
        except Exception:
            pass

    [record] = _lines(caplog)
    assert record.levelno == logging.ERROR
    assert (record.uri, record.status) == ("/stream", 200)
    assert record.error == "RuntimeError('minio dropped')"


def test_the_app_logs_its_requests(test_client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        test_client.get("/documents/summary")

    assert any(r.uri == "/documents/summary" for r in _lines(caplog))
