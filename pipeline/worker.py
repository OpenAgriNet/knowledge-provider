"""
Temporal worker for the OCR pipeline.
"""

import asyncio
import logging
import os

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import Worker

from . import db
from .activities import (
    auto_tag_chunks_from_db,
    create_chunks,
    create_chunks_from_db,
    detect_and_translate_pages,
    detect_and_translate_pages_from_db,
    ingest_document_from_db,
    ingest_to_vector_db,
    persist_document_content,
    prepare_for_ingestion,
    promote_document_to_prod_qdrant,
    publish_catalog_to_network,
    run_ocr,
    run_ocr_and_store,
    update_document_state,
)
from .activity_logging import ActivityLogging
from .network_group import validate_network_provider_config
from .observability import configure_console, configure_logs, reset_logs
from .workflows import (
    ChunkingOnlyWorkflow,
    DocumentPipelineWorkflow,
    OcrOnlyWorkflow,
    PromoteToProdWorkflow,
    ReingestionWorkflow,
    TranslationOnlyWorkflow,
)

LOG_LEVEL = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").strip().upper(), logging.INFO)

configure_console(LOG_LEVEL)

# Temporal appends its context to the message by default. It also sends it as
# an attribute, which a log search can filter on; the message copy only clutters.
activity.logger.activity_info_on_message = False
workflow.logger.workflow_info_on_message = False

# Set Temporal SDK logging to INFO regardless of LOG_LEVEL - its own DEBUG
# output is noisy and rarely what LOG_LEVEL=DEBUG is set to see.
logging.getLogger("temporalio").setLevel(logging.INFO)

TASK_QUEUE = "ocr-pipeline"
INTERCEPTORS = [ActivityLogging()]


async def main():
    """Start the worker."""
    configure_logs()
    try:
        await _run()
    except Exception:
        logging.exception("worker crashed")
        raise
    finally:
        logging.info("worker stopping")
        reset_logs()


async def _run():
    validate_network_provider_config()

    translation_provider = os.environ.get("TRANSLATION_PROVIDER", "gemma_vllm").strip().lower()
    if translation_provider in {"gemma_vllm", "gemma4", "gemma"}:
        if not os.environ.get("TRANSLATION_VLLM_BASE_URL", "").strip():
            logging.error("worker startup: TRANSLATION_VLLM_BASE_URL not set, exiting")
            return

    # Initialize SQLite database
    logging.info("worker startup: initializing SQLite database")
    db.init_db()

    temporal_host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    max_concurrent_activities = int(os.environ.get("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "4"))
    logging.info("worker startup: connecting to Temporal at %s", temporal_host)

    client = await Client.connect(temporal_host)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        max_concurrent_activities=max_concurrent_activities,
        interceptors=INTERCEPTORS,
        workflows=[
            DocumentPipelineWorkflow,
            ReingestionWorkflow,
            PromoteToProdWorkflow,
            TranslationOnlyWorkflow,
            OcrOnlyWorkflow,
            ChunkingOnlyWorkflow,
        ],
        activities=[
            run_ocr,
            run_ocr_and_store,
            create_chunks,
            create_chunks_from_db,
            auto_tag_chunks_from_db,
            prepare_for_ingestion,
            ingest_to_vector_db,
            ingest_document_from_db,
            promote_document_to_prod_qdrant,
            publish_catalog_to_network,
            update_document_state,
            detect_and_translate_pages,
            detect_and_translate_pages_from_db,
            persist_document_content,
        ],
    )

    logging.info(
        "worker started",
        extra={
            "queue": TASK_QUEUE,
            "temporal": temporal_host,
            "activity_concurrency": max_concurrent_activities,
        },
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
