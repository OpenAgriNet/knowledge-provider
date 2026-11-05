from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from temporalio import activity, workflow
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

logger = logging.getLogger(__name__)


class TemporalLogging(Interceptor):
    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _LogActivity(next)

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor]:
        return _LogWorkflow


class _LogActivity(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        fields = (info.activity_type, info.workflow_id, info.attempt)
        logger.info("activity started name=%s workflow_id=%s attempt=%s", *fields)
        started = time.perf_counter()
        try:
            result = await self.next.execute_activity(input)
        except asyncio.CancelledError:
            logger.warning(
                "activity cancelled name=%s workflow_id=%s attempt=%s duration_ms=%d",
                *fields,
                _elapsed_ms(started),
            )
            raise
        except Exception as exc:
            logger.warning(
                "activity failed name=%s workflow_id=%s attempt=%s duration_ms=%d error=%r",
                *fields,
                _elapsed_ms(started),
                exc,
            )
            raise
        logger.info(
            "activity completed name=%s workflow_id=%s attempt=%s duration_ms=%d",
            *fields,
            _elapsed_ms(started),
        )
        return result


class _LogWorkflow(WorkflowInboundInterceptor):
    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        info = workflow.info()
        fields = (info.workflow_type, info.workflow_id, info.run_id)
        workflow.logger.info("workflow started name=%s workflow_id=%s run_id=%s", *fields)
        try:
            result = await self.next.execute_workflow(input)
        except asyncio.CancelledError:
            workflow.logger.warning(
                "workflow cancelled name=%s workflow_id=%s run_id=%s", *fields
            )
            raise
        except Exception as exc:
            workflow.logger.error(
                "workflow failed name=%s workflow_id=%s run_id=%s error=%r",
                *fields,
                exc,
                exc_info=True,
            )
            raise
        workflow.logger.info("workflow completed name=%s workflow_id=%s run_id=%s", *fields)
        return result


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
