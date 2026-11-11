import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pipeline import worker
from pipeline.api import api


@pytest.fixture
def calls():
    return []


def test_worker_configures_logs_and_resets_them_on_exit(monkeypatch, calls):
    monkeypatch.setattr(worker, "configure_logs", lambda: calls.append("configure"))
    monkeypatch.setattr(worker, "reset_logs", lambda: calls.append("reset"))
    monkeypatch.setattr(worker, "_run", AsyncMock(side_effect=lambda: calls.append("run")))

    asyncio.run(worker.main())

    assert calls == ["configure", "run", "reset"]


def test_worker_resets_logs_when_it_fails(monkeypatch, calls):
    monkeypatch.setattr(worker, "configure_logs", lambda: calls.append("configure"))
    monkeypatch.setattr(worker, "reset_logs", lambda: calls.append("reset"))
    monkeypatch.setattr(worker, "_run", AsyncMock(side_effect=RuntimeError("temporal down")))

    with pytest.raises(RuntimeError):
        asyncio.run(worker.main())

    assert calls == ["configure", "reset"]


def test_api_lifespan_configures_logs_first_and_resets_them_on_shutdown(monkeypatch, calls, caplog):
    monkeypatch.setattr(api, "configure_logs", lambda: calls.append("configure"))
    monkeypatch.setattr(api, "reset_logs", lambda: calls.append("reset"))
    monkeypatch.setattr(api, "validate_network_provider_config", lambda: calls.append("validate"))
    monkeypatch.setattr(api, "load_auth_config", lambda: SimpleNamespace(disabled=True))
    monkeypatch.setattr(api, "validate_auth_config", lambda _cfg: None)
    monkeypatch.setattr(api.db, "init_db", lambda: None)
    monkeypatch.setattr(api.Client, "connect", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(api, "Minio", lambda *a, **k: MagicMock(bucket_exists=lambda _b: True))

    async def run():
        async with api.lifespan(api.app):
            calls.append("serving")

    monkeypatch.setenv("MINIO_ACCESS_KEY", "key")
    monkeypatch.setenv("MINIO_SECRET_KEY", "secret")

    with caplog.at_level("INFO"):
        asyncio.run(run())

    assert calls == ["configure", "validate", "serving", "reset"]
    assert any(r.getMessage().startswith("api started auth=disabled") for r in caplog.records)


def test_worker_crash_is_logged_with_its_traceback(monkeypatch, calls, caplog):
    monkeypatch.setattr(worker, "configure_logs", lambda: None)
    monkeypatch.setattr(worker, "reset_logs", lambda: calls.append("reset"))
    monkeypatch.setattr(worker, "_run", AsyncMock(side_effect=ConnectionError("temporal down")))

    with caplog.at_level("INFO"), pytest.raises(ConnectionError):
        asyncio.run(worker.main())

    [crash] = [r for r in caplog.records if r.getMessage() == "worker crashed"]
    assert crash.levelname == "ERROR" and isinstance(crash.exc_info[1], ConnectionError)
    assert calls == ["reset"]


def test_worker_logs_startup_and_hands_its_interceptors_to_temporal(monkeypatch, caplog):
    built = {}

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            built.update(kwargs)

        async def run(self):
            return None

    monkeypatch.setenv("TRANSLATION_PROVIDER", "none")
    monkeypatch.setattr(worker, "validate_network_provider_config", lambda: None)
    monkeypatch.setattr(worker.db, "init_db", lambda: None)
    monkeypatch.setattr(worker.Client, "connect", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(worker, "Worker", FakeWorker)

    with caplog.at_level("INFO"):
        asyncio.run(worker._run())

    assert built["interceptors"] is worker.INTERCEPTORS
    assert any(r.getMessage().startswith("worker started queue=ocr-pipeline") for r in caplog.records)


def test_api_startup_failure_is_logged_and_flushed(monkeypatch, calls, caplog):
    monkeypatch.setattr(api, "configure_logs", lambda: None)
    monkeypatch.setattr(api, "reset_logs", lambda: calls.append("reset"))
    monkeypatch.setattr(api, "_start_services", AsyncMock(side_effect=RuntimeError("MINIO keys missing")))

    async def run():
        async with api.lifespan(api.app):
            calls.append("serving")

    with caplog.at_level("INFO"), pytest.raises(RuntimeError):
        asyncio.run(run())

    [failed] = [r for r in caplog.records if r.getMessage() == "api startup failed"]
    assert failed.levelname == "ERROR" and isinstance(failed.exc_info[1], RuntimeError)
    assert calls == ["reset"]
