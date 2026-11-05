import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.testing import ActivityEnvironment

from pipeline import activity_logging, worker

LOGGER = "pipeline.activity_logging"


def _interceptor(next_result=None, error=None):
    following = MagicMock()
    following.execute_activity = AsyncMock(return_value=next_result, side_effect=error)
    return activity_logging.ActivityLogging().intercept_activity(following)


def _messages(caplog):
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == LOGGER]


@pytest.mark.asyncio
async def test_logs_start_and_completion(caplog):
    interceptor = _interceptor(next_result="done")

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await ActivityEnvironment().run(interceptor.execute_activity, MagicMock())

    assert result == "done"
    [(start_level, start), (end_level, end)] = _messages(caplog)
    assert start_level == end_level == logging.INFO
    assert start.startswith("activity started name=unknown")
    assert "workflow_id=test" in start and "attempt=1" in start
    assert end.startswith("activity completed name=unknown") and "duration_ms=" in end


@pytest.mark.asyncio
async def test_logs_failure_and_reraises(caplog):
    interceptor = _interceptor(error=ValueError("bad page"))

    with caplog.at_level(logging.INFO, logger=LOGGER):
        with pytest.raises(ValueError):
            await ActivityEnvironment().run(interceptor.execute_activity, MagicMock())

    [_, (level, failed)] = _messages(caplog)
    assert level == logging.ERROR
    assert failed.startswith("activity failed name=unknown")
    assert "error=ValueError('bad page')" in failed


def test_worker_registers_the_interceptor():
    assert any(isinstance(i, activity_logging.ActivityLogging) for i in worker.INTERCEPTORS)
