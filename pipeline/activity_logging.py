from __future__ import annotations

import logging
import time
from typing import Any

from temporalio import activity
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

logger = logging.getLogger(__name__)


class ActivityLogging(Interceptor):
    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _LogActivity(next)


class _LogActivity(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        fields = (info.activity_type, info.workflow_id, info.attempt)
        logger.info("activity started name=%s workflow_id=%s attempt=%s", *fields)
        started = time.perf_counter()
        try:
            result = await self.next.execute_activity(input)
        except Exception as exc:
            logger.error(
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


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
