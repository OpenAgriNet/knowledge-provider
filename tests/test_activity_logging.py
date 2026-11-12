import asyncio
import logging
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from pipeline import activity_logging

LOGGER = "pipeline.activity_logging"


def _interceptor(next_result=None, error=None):
    following = MagicMock()
    following.execute_activity = AsyncMock(return_value=next_result, side_effect=error)
    return activity_logging.ActivityLogging().intercept_activity(following)


def _records(caplog, name=LOGGER):
    return [r for r in caplog.records if r.name == name]


@pytest.mark.asyncio
async def test_activity_logs_start_and_completion(caplog):
    interceptor = _interceptor(next_result="done")

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await ActivityEnvironment().run(interceptor.execute_activity, MagicMock())

    assert result == "done"
    [start, end] = _records(caplog)
    assert start.levelno == end.levelno == logging.INFO
    assert start.getMessage() == "activity started"
    assert (start.activity_type, start.workflow_id, start.attempt) == ("unknown", "test", 1)
    assert end.getMessage() == "activity completed"
    assert isinstance(end.duration_ms, int)


@pytest.mark.asyncio
async def test_activity_failure_is_logged_and_reraised(caplog):
    interceptor = _interceptor(error=ValueError("bad page"))

    with caplog.at_level(logging.INFO, logger=LOGGER):
        with pytest.raises(ValueError):
            await ActivityEnvironment().run(interceptor.execute_activity, MagicMock())

    [_, failed] = _records(caplog)
    assert failed.levelno == logging.WARNING
    assert failed.getMessage() == "activity failed"
    assert failed.error == "ValueError('bad page')"


@pytest.mark.asyncio
async def test_activity_cancellation_logs_an_end(caplog):
    interceptor = _interceptor(error=asyncio.CancelledError())

    with caplog.at_level(logging.INFO, logger=LOGGER):
        with pytest.raises(asyncio.CancelledError):
            await ActivityEnvironment().run(interceptor.execute_activity, MagicMock())

    [_, cancelled] = _records(caplog)
    assert cancelled.levelno == logging.WARNING
    assert cancelled.getMessage() == "activity cancelled"


@activity.defn
async def _echo(value: str) -> str:
    return value


@workflow.defn(sandboxed=False)
class _Succeeds:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(_echo, "ok", start_to_close_timeout=timedelta(seconds=10))


@workflow.defn
class _Fails:
    @workflow.run
    async def run(self) -> None:
        raise ApplicationError("chunking rejected", non_retryable=True)


@workflow.defn
class _WaitsForever:
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)


async def _run_workflow(workflow_cls):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="logging-test",
            workflows=[workflow_cls],
            activities=[_echo],
            interceptors=[activity_logging.ActivityLogging()],
        ):
            return await env.client.execute_workflow(
                workflow_cls.run, id=f"wf-{uuid.uuid4()}", task_queue="logging-test"
            )


@pytest.mark.asyncio
@pytest.mark.workflow
async def test_workflow_and_its_activities_log_start_and_completion(caplog):
    with caplog.at_level(logging.INFO):
        assert await _run_workflow(_Succeeds) == "ok"

    logged = {
        (r.getMessage(), getattr(r, "workflow_type", None) or getattr(r, "activity_type", None))
        for r in caplog.records
    }
    for expected in (
        ("workflow started", "_Succeeds"),
        ("activity started", "_echo"),
        ("activity completed", "_echo"),
        ("workflow completed", "_Succeeds"),
    ):
        assert expected in logged, expected


@pytest.mark.asyncio
@pytest.mark.workflow
async def test_workflow_failure_is_logged_with_its_error(caplog):
    with caplog.at_level(logging.INFO):
        with pytest.raises(Exception):
            await _run_workflow(_Fails)

    [failed] = [r for r in caplog.records if r.getMessage().startswith("workflow failed")]
    assert failed.levelno == logging.ERROR
    assert failed.workflow_type == "_Fails"
    assert "chunking rejected" in failed.error
    assert failed.exc_info is not None


@pytest.mark.asyncio
@pytest.mark.workflow
async def test_workflow_cancellation_logs_an_end(caplog):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="logging-test",
            workflows=[_WaitsForever],
            interceptors=[activity_logging.ActivityLogging()],
        ):
            with caplog.at_level(logging.INFO):
                handle = await env.client.start_workflow(
                    _WaitsForever.run, id=f"wf-{uuid.uuid4()}", task_queue="logging-test"
                )
                await handle.cancel()
                with pytest.raises(Exception):
                    await handle.result()

    [cancelled] = [r for r in caplog.records if r.getMessage().startswith("workflow cancelled")]
    assert cancelled.levelno == logging.WARNING
    assert cancelled.workflow_type == "_WaitsForever"


def test_temporal_context_stays_out_of_the_message():
    # It still arrives as an attribute; in the message it only clutters the body.
    assert activity.logger.activity_info_on_message is False
    assert workflow.logger.workflow_info_on_message is False
