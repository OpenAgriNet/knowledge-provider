"""Orchestrate intake and processing of a document."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from pipeline.documents.models import DocumentStage
    from pipeline.temporal.activities.indexing import (
        ingest_document_from_db,
        promote_document_to_prod_qdrant,
    )
    from pipeline.temporal.activities.processing import (
        create_chunks_from_db,
        detect_and_translate_pages_from_db,
    )
    from pipeline.temporal.activities.publication import publish_catalog_to_network
    from pipeline.temporal.activities.source import run_ocr_and_store
    from pipeline.temporal.activities.state import auto_tag_chunks_from_db

from .policies import (
    CHUNK_RETRY,
    INGEST_RETRY,
    NETWORK_PUBLISH_RETRY,
    OCR_RETRY,
    TRANSLATION_RETRY,
    _mirror_state,
    _now_iso,
)


@dataclass
class DocumentWorkflowState:
    """Workflow state - queryable and modifiable via signals."""

    document_id: str
    filename: str
    filepath: str

    stage: DocumentStage = DocumentStage.REGISTERED
    error_message: Optional[str] = None

    page_count: int = 0
    chunk_count: int = 0
    translated_count: int = 0

    ocr_approved: bool = False
    translation_approved: bool = False
    chunks_approved: bool = False
    ingestion_approved: bool = False
    prod_approved: bool = False
    network_published: bool = False

    chunk_size: int = 450
    chunk_overlap: int = 128
    min_tokens: int = 100
    index_name: str = "documents-index"
    stop_after_ocr: bool = False

    created_at: str = ""
    ocr_completed_at: Optional[str] = None
    translation_completed_at: Optional[str] = None
    chunks_completed_at: Optional[str] = None
    ingested_at: Optional[str] = None
    promoted_to_prod_at: Optional[str] = None


@workflow.defn
class DocumentPipelineWorkflow:
    """Main document processing workflow."""

    def __init__(self):
        self.state = None

    @workflow.run
    async def run(
        self,
        document_id: str,
        filename: str,
        filepath: str,
        chunk_size: int = 450,
        chunk_overlap: int = 128,
        min_tokens: int = 100,
        index_name: str = "documents-index",
        auto_approve: bool = False,
        stop_after_ocr: bool = False,
        skip_prod: bool = False,
        release_network_id: str | None = None,
        parser_provider: str | None = None,
        index_only: bool = False,
    ) -> dict:
        self.state = DocumentWorkflowState(
            document_id=document_id,
            filename=filename,
            filepath=filepath,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            min_tokens=min_tokens,
            index_name=index_name,
            stop_after_ocr=stop_after_ocr,
            created_at=_now_iso(),
        )

        try:
            self.state.stage = DocumentStage.OCR_PROCESSING
            workflow.logger.info(f"Starting OCR for {filename}")

            await _mirror_state(workflow.info().workflow_id, "ocr_processing", 0, 0, None)

            ocr_args = [workflow.info().workflow_id, filepath]
            if parser_provider:
                ocr_args.append(parser_provider)
            ocr_result = await workflow.execute_activity(
                run_ocr_and_store,
                args=ocr_args,
                start_to_close_timeout=timedelta(minutes=90),
                heartbeat_timeout=timedelta(minutes=10),
                retry_policy=OCR_RETRY,
            )
            self.state.page_count = ocr_result.get("page_count", 0)
            self.state.ocr_completed_at = _now_iso()
            self.state.stage = DocumentStage.OCR_REVIEW

            await _mirror_state(workflow.info().workflow_id, "ocr_review", self.state.page_count, 0, None)

            if stop_after_ocr:
                workflow.logger.info(f"OCR-only run complete for {filename}")
                return {
                    "document_id": document_id,
                    "filename": filename,
                    "stage": self.state.stage.value,
                    "pages": self.state.page_count,
                    "chunks": 0,
                    "stop_after_ocr": True,
                }

            if not auto_approve:
                await workflow.wait_condition(lambda: self.state.ocr_approved)

            self.state.stage = DocumentStage.TRANSLATION_PROCESSING
            await _mirror_state(workflow.info().workflow_id, "translation_processing", self.state.page_count, 0, None)

            translation_result = await workflow.execute_activity(
                detect_and_translate_pages_from_db,
                args=[workflow.info().workflow_id],
                start_to_close_timeout=timedelta(minutes=90),
                retry_policy=TRANSLATION_RETRY,
            )
            self.state.page_count = translation_result.get("page_count", self.state.page_count)
            self.state.translated_count = translation_result.get("translated_count", 0)
            self.state.translation_completed_at = _now_iso()
            self.state.stage = DocumentStage.TRANSLATION_REVIEW

            await _mirror_state(workflow.info().workflow_id, "translation_review", self.state.page_count, 0, None)

            if not auto_approve:
                await workflow.wait_condition(lambda: self.state.translation_approved)

            self.state.stage = DocumentStage.CHUNKING
            await _mirror_state(workflow.info().workflow_id, "chunking", self.state.page_count, 0, None)

            source_first = index_only or bool(release_network_id and workflow.patched("source-first-release-v1"))
            if not index_only and not release_network_id and (filepath.startswith("intake://") or filename.lower().endswith(".json")):
                source_first = workflow.patched("source-first-structured-draft-v1")
            chunk_args = [workflow.info().workflow_id, chunk_size, chunk_overlap, min_tokens]
            if source_first:
                chunk_args.append(True)
            chunk_result = await workflow.execute_activity(
                create_chunks_from_db,
                args=chunk_args,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=CHUNK_RETRY,
            )
            self.state.chunk_count = chunk_result.get("chunk_count", 0)
            self.state.chunks_completed_at = _now_iso()

            if workflow.patched("auto-tag-v1"):
                await workflow.execute_activity(
                    auto_tag_chunks_from_db,
                    args=[workflow.info().workflow_id, filename],
                    start_to_close_timeout=timedelta(minutes=45),
                    retry_policy=CHUNK_RETRY,
                )

            self.state.stage = DocumentStage.CHUNK_REVIEW

            await _mirror_state(workflow.info().workflow_id, "chunk_review", self.state.page_count, self.state.chunk_count, None)

            if not auto_approve:
                await workflow.wait_condition(lambda: self.state.chunks_approved)

            self.state.stage = DocumentStage.READY_FOR_INGESTION
            await _mirror_state(workflow.info().workflow_id, "ready_for_ingestion", self.state.page_count, self.state.chunk_count, None)

            if not auto_approve:
                await workflow.wait_condition(lambda: self.state.ingestion_approved)

            self.state.stage = DocumentStage.INGESTING
            await _mirror_state(workflow.info().workflow_id, "ingesting", self.state.page_count, self.state.chunk_count, None)

            ingest_args = [workflow.info().workflow_id, document_id, filename, index_name]
            if release_network_id:
                ingest_args.extend([10, release_network_id])
                if source_first:
                    ingest_args.append(True)
            elif workflow.patched("freeze-draft-revision-v1"):
                ingest_args.extend([10, None, source_first, True])
            result = await workflow.execute_activity(
                ingest_document_from_db,
                args=ingest_args,
                start_to_close_timeout=timedelta(minutes=90),
                retry_policy=INGEST_RETRY,
            )

            self.state.ingested_at = _now_iso()

            if release_network_id:
                self.state.stage = DocumentStage.PUBLISHING_TO_NETWORK
                await _mirror_state(
                    workflow.info().workflow_id, "publishing_to_network", self.state.page_count,
                    self.state.chunk_count, None,
                )
                await workflow.execute_activity(
                    publish_catalog_to_network,
                    args=[workflow.info().workflow_id, str(workflow.uuid4()), release_network_id],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=NETWORK_PUBLISH_RETRY,
                )
                self.state.network_published = True
                self.state.stage = DocumentStage.COMPLETED
                await _mirror_state(
                    workflow.info().workflow_id, "completed", self.state.page_count,
                    self.state.chunk_count, None,
                )
                return {
                    "document_id": document_id,
                    "filename": filename,
                    "stage": self.state.stage.value,
                    "pages": self.state.page_count,
                    "chunks": self.state.chunk_count,
                    "records_ingested": result.get("records_ingested", 0),
                    "release_network_id": release_network_id,
                }

            if not workflow.patched("publish-after-prod-v1"):
                # Old order, preserved for in-flight workflows whose history
                # already recorded this activity immediately after DEV ingest.
                # New workflows publish only after a successful PROD
                # promotion instead (see below).
                self.state.stage = DocumentStage.PUBLISHING_TO_NETWORK
                await _mirror_state(
                    workflow.info().workflow_id,
                    "publishing_to_network",
                    self.state.page_count,
                    self.state.chunk_count,
                    None,
                )

                # transactionId is generated once here (workflow.uuid4() is
                # replay-safe) and reused across activity retries; the activity
                # generates a fresh messageId on every attempt.
                network_transaction_id = str(workflow.uuid4())
                await workflow.execute_activity(
                    publish_catalog_to_network,
                    args=[workflow.info().workflow_id, network_transaction_id],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=NETWORK_PUBLISH_RETRY,
                )
                self.state.network_published = True

            # DISABLE_PROD_SETTING: finish at DEV ingest. Decided when the
            # workflow was started and passed in as an argument, so a replay
            # always takes the same branch as the original run.
            if skip_prod:
                self.state.stage = DocumentStage.COMPLETED
                await _mirror_state(
                    workflow.info().workflow_id,
                    "completed",
                    self.state.page_count,
                    self.state.chunk_count,
                    None,
                )
                workflow.logger.info(
                    f"Pipeline complete for {filename} (PROD stages disabled)"
                )
                return {
                    "document_id": document_id,
                    "filename": filename,
                    "stage": self.state.stage.value,
                    "pages": self.state.page_count,
                    "chunks": self.state.chunk_count,
                    "records_ingested": result.get("records_ingested", 0),
                    "records_promoted_to_prod": 0,
                    "prod_skipped": True,
                }

            self.state.stage = DocumentStage.APPROVAL_FOR_PROD
            await _mirror_state(
                workflow.info().workflow_id,
                "approval_for_prod",
                self.state.page_count,
                self.state.chunk_count,
                None,
            )

            # Prod promotion is always an explicit superadmin action (even with auto_approve).
            await workflow.wait_condition(lambda: self.state.prod_approved)

            self.state.stage = DocumentStage.INGESTING_PROD
            await _mirror_state(
                workflow.info().workflow_id,
                "ingesting_prod",
                self.state.page_count,
                self.state.chunk_count,
                None,
            )

            prod_result = await workflow.execute_activity(
                promote_document_to_prod_qdrant,
                args=[workflow.info().workflow_id, document_id, filename],
                start_to_close_timeout=timedelta(minutes=90),
                retry_policy=INGEST_RETRY,
            )

            self.state.promoted_to_prod_at = _now_iso()

            if not self.state.network_published:
                # New order: publish to the network only now that PROD
                # promotion has actually succeeded. Skipped for workflows
                # that already published at the old position above.
                self.state.stage = DocumentStage.PUBLISHING_TO_NETWORK
                await _mirror_state(
                    workflow.info().workflow_id,
                    "publishing_to_network",
                    self.state.page_count,
                    self.state.chunk_count,
                    None,
                )

                network_transaction_id = str(workflow.uuid4())
                await workflow.execute_activity(
                    publish_catalog_to_network,
                    args=[workflow.info().workflow_id, network_transaction_id],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=NETWORK_PUBLISH_RETRY,
                )
                self.state.network_published = True

            self.state.stage = DocumentStage.COMPLETED

            await _mirror_state(workflow.info().workflow_id, "completed", self.state.page_count, self.state.chunk_count, None)

            workflow.logger.info(f"Pipeline complete for {filename}")
            return {
                "document_id": document_id,
                "filename": filename,
                "stage": self.state.stage.value,
                "pages": self.state.page_count,
                "chunks": self.state.chunk_count,
                "records_ingested": result.get("records_ingested", 0),
                "records_promoted_to_prod": prod_result.get("records_ingested", 0),
            }

        except Exception as e:
            self.state.stage = DocumentStage.FAILED
            self.state.error_message = str(e)
            workflow.logger.error(f"Pipeline failed: {e}")

            try:
                await _mirror_state(workflow.info().workflow_id, "failed", self.state.page_count, self.state.chunk_count, str(e))
            except Exception:
                pass
            raise

    @workflow.query
    def get_state(self) -> dict:
        if not self.state:
            return {}
        return {
            "document_id": self.state.document_id,
            "filename": self.state.filename,
            "filepath": self.state.filepath,
            "stage": self.state.stage.value,
            "error_message": self.state.error_message,
            "page_count": self.state.page_count,
            "chunk_count": self.state.chunk_count,
            "translated_count": self.state.translated_count,
            "ocr_approved": self.state.ocr_approved,
            "translation_approved": self.state.translation_approved,
            "chunks_approved": self.state.chunks_approved,
            "ingestion_approved": self.state.ingestion_approved,
            "prod_approved": self.state.prod_approved,
            "created_at": self.state.created_at,
            "ocr_completed_at": self.state.ocr_completed_at,
            "translation_completed_at": self.state.translation_completed_at,
            "chunks_completed_at": self.state.chunks_completed_at,
            "ingested_at": self.state.ingested_at,
            "promoted_to_prod_at": self.state.promoted_to_prod_at,
        }

    @workflow.signal
    def approve_ocr(self):
        self.state.ocr_approved = True

    @workflow.signal
    def approve_translation(self):
        self.state.translation_approved = True

    @workflow.signal
    def approve_chunks(self):
        self.state.chunks_approved = True

    @workflow.signal
    def approve_ingestion(self):
        self.state.ingestion_approved = True

    @workflow.signal
    def approve_prod(self):
        self.state.prod_approved = True
