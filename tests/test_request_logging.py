import logging

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from pipeline.api import request_logging

LOGGER = "pipeline.api.requests"


@pytest.fixture
def client():
    app = FastAPI()
    request_logging.install(app)

    @app.get("/ok")
    def ok():
        return {"secret": "body"}

    @app.get("/missing")
    def missing():
        raise HTTPException(404, "Document not found")

    @app.get("/boom")
    def boom():
        raise RuntimeError("database locked")

    @app.get("/typed/{n}")
    def typed(n: int):
        return {"n": n}

    return TestClient(app, raise_server_exceptions=False)


def _lines(caplog):
    return [r for r in caplog.records if r.name == LOGGER]


def test_success_logs_method_uri_and_status_without_body(client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        client.get("/ok?index=documents")

    [record] = _lines(caplog)
    message = record.getMessage()
    assert record.levelno == logging.INFO
    assert "method=GET uri=/ok?index=documents status=200" in message
    assert "duration_ms=" in message
    assert "secret" not in message


def test_error_response_logs_status_and_error_detail(client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        response = client.get("/missing")

    assert response.json() == {"detail": "Document not found"}
    [record] = _lines(caplog)
    assert record.levelno == logging.WARNING
    assert "uri=/missing status=404" in record.getMessage()
    assert "error='Document not found'" in record.getMessage()


def test_validation_error_logs_422_with_its_detail(client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        response = client.get("/typed/abc")

    assert response.status_code == 422
    [record] = _lines(caplog)
    assert "status=422" in record.getMessage()
    assert "int_parsing" in record.getMessage()


def test_unknown_route_logs_404(client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        client.get("/nowhere")

    [record] = _lines(caplog)
    assert "uri=/nowhere status=404" in record.getMessage()
    assert "error='Not Found'" in record.getMessage()


def test_unhandled_exception_logs_500_with_traceback(client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        response = client.get("/boom")

    assert response.status_code == 500
    [record] = _lines(caplog)
    assert record.levelno == logging.ERROR
    assert "uri=/boom status=500" in record.getMessage()
    assert "error=RuntimeError('database locked')" in record.getMessage()
    assert record.exc_info is not None


def test_the_app_logs_its_requests(test_client, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        test_client.get("/health")

    assert any("uri=/health status=200" in r.getMessage() for r in _lines(caplog))
