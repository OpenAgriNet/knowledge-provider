"""Start a previously registered document workflow exactly once."""

import asyncio

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from pipeline import db
from pipeline.temporal.workflows import DocumentPipelineWorkflow


async def start_registered_document(client, task_queue: str, job: dict) -> None:
    try:
        await client.start_workflow(
            DocumentPipelineWorkflow.run,
            args=job["config"]["upload_start_args"],
            id=job["temporal_workflow_id"],
            task_queue=task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        # A prior dispatch reached Temporal even if its response was lost.
        pass
    await asyncio.to_thread(db.mark_upload_started, job["id"])
