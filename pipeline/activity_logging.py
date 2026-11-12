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

# Temporal appends its context to the message by default. It also sends it as
# an attribute, which a log search can filter on; the message copy only clutters.
activity.logger.activity_info_on_message = False
workflow.logger.workflow_info_on_message = False


class ActivityLogging(Interceptor):
    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _LogActivity(next)

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor]:
        return _LogWorkflow


class _LogActivity(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        fields = {
            "activity_type": info.activity_type,
            "workflow_id": info.workflow_id,
            "attempt": info.attempt,
        }
        logger.info("activity started", extra=fields)
        started = time.perf_counter()
        try:
            result = await self.next.execute_activity(input)
        except asyncio.CancelledError:
            logger.warning(
                "activity cancelled", extra={**fields, "duration_ms": _elapsed_ms(started)}
            )
            raise
        except Exception as exc:
            logger.warning(
                "activity failed",
                extra={**fields, "duration_ms": _elapsed_ms(started), "error": repr(exc)},
            )
            raise
        logger.info(
            "activity completed", extra={**fields, "duration_ms": _elapsed_ms(started)}
        )
        return result


class _LogWorkflow(WorkflowInboundInterceptor):
    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        info = workflow.info()
        fields = {
            "workflow_type": info.workflow_type,
            "workflow_id": info.workflow_id,
            "run_id": info.run_id,
        }
        workflow.logger.info("workflow started", extra=fields)
        try:
            result = await self.next.execute_workflow(input)
        except asyncio.CancelledError:
            workflow.logger.warning("workflow cancelled", extra=fields)
            raise
        except Exception as exc:
            workflow.logger.error(
                "workflow failed", extra={**fields, "error": repr(exc)}, exc_info=True
            )
            raise
        workflow.logger.info("workflow completed", extra=fields)
        return result


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
