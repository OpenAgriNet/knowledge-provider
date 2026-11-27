"""Retry policies, workflow clock and durable state mirroring."""

import os
from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from pipeline.temporal.activities.state import update_document_state


def _now_iso() -> str:
    """Get current time as ISO string (workflow-safe)."""
    return workflow.now().isoformat()


OCR_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=15),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=15),
    maximum_attempts=20,
)

CHUNK_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=1.5,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=10,
)

INGEST_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=10),
    maximum_attempts=15,
)

STATE_UPDATE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=3),
    backoff_coefficient=1.5,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=10,
)

TRANSLATION_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=15),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=15),
    maximum_attempts=20,
)

# Shorter than INGEST_RETRY: the Discovery Service is a new, unproven external
# dependency, so we fail the document faster rather than holding it in-flight
# for hours. Configurable (unlike the other policies above) since this
# dependency's real-world retry behavior is still unproven.
def _network_publish_retry_policy() -> RetryPolicy:
    return RetryPolicy(
        initial_interval=timedelta(
            seconds=int(os.environ.get("DISCOVERY_SERVICE_PUBLISH_INITIAL_INTERVAL_SECONDS", "30"))
        ),
        backoff_coefficient=float(
            os.environ.get("DISCOVERY_SERVICE_PUBLISH_BACKOFF_COEFFICIENT", "2.0")
        ),
        maximum_interval=timedelta(
            seconds=int(os.environ.get("DISCOVERY_SERVICE_PUBLISH_MAX_INTERVAL_SECONDS", "300"))
        ),
        maximum_attempts=int(os.environ.get("DISCOVERY_SERVICE_PUBLISH_MAX_ATTEMPTS", "5")),
    )


NETWORK_PUBLISH_RETRY = _network_publish_retry_policy()

async def _mirror_state(
    workflow_id: str,
    stage: str,
    page_count: int = 0,
    chunk_count: int = 0,
    error_message: Optional[str] = None,
) -> dict:
    """
    Mirror workflow state into Postgres as a local activity.

    Local activities avoid starving tiny state updates behind long-running OCR,
    translation, or chunking activities on the shared task queue.
    """
    if workflow.patched("local-state-update-v1"):
        return await workflow.execute_local_activity(
            update_document_state,
            args=[workflow_id, stage, page_count, chunk_count, error_message],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=STATE_UPDATE_RETRY,
        )

    return await workflow.execute_activity(
        update_document_state,
        args=[workflow_id, stage, page_count, chunk_count, error_message],
        start_to_close_timeout=timedelta(seconds=30),
        retry_policy=STATE_UPDATE_RETRY,
    )
