"""
Temporal worker for the OCR pipeline.
"""

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from pipeline import db
from pipeline.documents.document_formats import installed_formats
from pipeline.documents.document_kinds import configured_document_kinds
from pipeline.intake.scheduled_intake import poll_due_sources
from pipeline.intake.source_intake import reconcile_pending_sources
from pipeline.metadata.intake_profiles import load_intake_profiles
from pipeline.network.network_publisher import validate_network_adapters
from pipeline.network.release_policy import validate_projection_destinations
from pipeline.telemetry.logs import configure_logs, reset_logs
from pipeline.temporal.activities.indexing import (
    ingest_document_from_db,
    ingest_to_vector_db,
    prepare_for_ingestion,
    promote_document_to_prod_qdrant,
)
from pipeline.temporal.activities.logging import ActivityLogging
from pipeline.temporal.activities.processing import (
    create_chunks,
    create_chunks_from_db,
    detect_and_translate_pages,
    detect_and_translate_pages_from_db,
)
from pipeline.temporal.activities.publication import (
    publish_catalog_to_network,
    release_saved_revision,
)
from pipeline.temporal.activities.source import run_ocr, run_ocr_and_store
from pipeline.temporal.activities.state import (
    auto_tag_chunks_from_db,
    persist_document_content,
    update_document_state,
)
from pipeline.temporal.workflows import (
    ChunkingOnlyWorkflow,
    DocumentPipelineWorkflow,
    OcrOnlyWorkflow,
    PromoteToProdWorkflow,
    ReingestionWorkflow,
    SelectNetworkRevisionWorkflow,
    TranslationOnlyWorkflow,
)
from pipeline.translation.service import (
    PROVIDERS as TRANSLATION_PROVIDERS,
)
from pipeline.translation.service import (
    configured_translation_endpoint,
    configured_translation_type,
)

LOG_LEVEL = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").strip().upper(), logging.INFO)

# Configure verbose logging
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)

# Set Temporal SDK logging to INFO regardless of LOG_LEVEL - its own DEBUG
# output is noisy and rarely what LOG_LEVEL=DEBUG is set to see.
logging.getLogger("temporalio").setLevel(logging.INFO)

TASK_QUEUE = os.environ.get("TEMPORAL_TASK_QUEUE", "ocr-pipeline")
INTERCEPTORS = [ActivityLogging()]


async def _recover_source_starts(client: Client) -> None:
    while True:
        try:
            started = await reconcile_pending_sources(client, TASK_QUEUE)
            if started:
                logging.info("Recovered %d source intake starts", started)
        except Exception as error:
            logging.warning("Source intake start recovery failed error_type=%s", type(error).__name__)
        await asyncio.sleep(12)


async def _poll_scheduled_sources() -> None:
    while True:
        try:
            await poll_due_sources()
        except Exception as error:
            logging.warning("Scheduled pull polling failed error_type=%s", type(error).__name__)
        await asyncio.sleep(12)


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
    validate_network_adapters()
    installed_formats()
    load_intake_profiles()
    configured_document_kinds()
    validate_projection_destinations()

    if configured_translation_type() in TRANSLATION_PROVIDERS and not configured_translation_endpoint():
        raise RuntimeError("TRANSLATION_BASE_URL is required for the configured translation provider")

    logging.info("worker startup: initializing PostgreSQL database")
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
            SelectNetworkRevisionWorkflow,
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
            release_saved_revision,
            update_document_state,
            detect_and_translate_pages,
            detect_and_translate_pages_from_db,
            persist_document_content,
        ],
    )


    logging.info(
        "worker started queue=%s temporal=%s activity_concurrency=%s",
        TASK_QUEUE,
        temporal_host,
        max_concurrent_activities,
    )
    recovery = asyncio.create_task(_recover_source_starts(client))
    pulls = asyncio.create_task(_poll_scheduled_sources())
    try:
        await worker.run()
    finally:
        recovery.cancel()
        pulls.cancel()
        await asyncio.gather(recovery, pulls, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
