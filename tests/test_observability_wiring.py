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


def test_api_lifespan_configures_logs_first_and_resets_them_on_shutdown(monkeypatch, calls):
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

    asyncio.run(run())

    assert calls == ["configure", "validate", "serving", "reset"]
