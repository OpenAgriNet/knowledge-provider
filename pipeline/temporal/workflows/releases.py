"""Reindex a document or publish a selected revision."""

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
    from pipeline.temporal.activities.publication import (
        publish_catalog_to_network,
        release_saved_revision,
    )

from .policies import (
    INGEST_RETRY,
    NETWORK_PUBLISH_RETRY,
    _mirror_state,
)


@workflow.defn
class SelectNetworkRevisionWorkflow:
    @workflow.run
    async def run(
        self, document_id: str, revision_id: str, network_id: str, expected_generation: str | None,
    ) -> dict:
        return await workflow.execute_activity(
            release_saved_revision,
            args=[document_id, revision_id, network_id, str(workflow.uuid4()), expected_generation],
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=NETWORK_PUBLISH_RETRY,
        )

@dataclass
class ReingestionWorkflowState:
    document_id: str
    filename: str
    workflow_id: str
    stage: DocumentStage = DocumentStage.READY_FOR_INGESTION
    error_message: Optional[str] = None
    chunk_count: int = 0
    records_ingested: int = 0


@workflow.defn
class ReingestionWorkflow:
    """Lightweight workflow for re-ingesting completed documents to the vector index."""

    def __init__(self):
        self.state = None

    @workflow.run
    async def run(
        self,
        document_id: str,
        filename: str,
        original_workflow_id: str,
        page_count: int = 0,
        chunk_count: int = 0,
        index_name: str = "documents-index",
        index_only: bool = False,
    ) -> dict:
        self.state = ReingestionWorkflowState(
            document_id=document_id,
            filename=filename,
            workflow_id=original_workflow_id,
            chunk_count=chunk_count,
        )

        try:
            await _mirror_state(original_workflow_id, "ingesting", page_count, chunk_count, None)

            self.state.stage = DocumentStage.INGESTING
            ingest_args = [original_workflow_id, document_id, filename, index_name]
            if index_only:
                ingest_args.extend([10, None, True])
                if workflow.patched("freeze-index-only-reingest-v1"):
                    ingest_args.extend([True, workflow.info().workflow_id])
            result = await workflow.execute_activity(
                ingest_document_from_db,
                args=ingest_args,
                start_to_close_timeout=timedelta(minutes=90),
                retry_policy=INGEST_RETRY,
            )

            self.state.records_ingested = result.get("records_ingested", 0)

            if not index_only and not workflow.patched("publish-after-prod-v1"):
                # Old order, preserved for in-flight workflows whose history
                # already recorded this activity here. New executions publish
                # to the network only from PromoteToProdWorkflow, once this
                # reingest's own PROD promotion actually succeeds - this
                # workflow never performs that promotion itself.
                self.state.stage = DocumentStage.PUBLISHING_TO_NETWORK
                await _mirror_state(original_workflow_id, "publishing_to_network", page_count, chunk_count, None)

                # Content changed on reingest, so the network gets a fresh publish
                # with a new transactionId (same replay-safety rule as above).
                network_transaction_id = str(workflow.uuid4())
                await workflow.execute_activity(
                    publish_catalog_to_network,
                    args=[original_workflow_id, network_transaction_id],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=NETWORK_PUBLISH_RETRY,
                )

            self.state.stage = DocumentStage.COMPLETED if index_only else DocumentStage.APPROVAL_FOR_PROD
            await _mirror_state(original_workflow_id, self.state.stage.value, page_count, chunk_count, None)

            return {
                "document_id": document_id,
                "filename": filename,
                "stage": self.state.stage.value,
                "chunks": chunk_count,
                "records_ingested": self.state.records_ingested,
            }

        except Exception as e:
            self.state.stage = DocumentStage.FAILED
            self.state.error_message = str(e)
            try:
                await _mirror_state(original_workflow_id, "failed", page_count, chunk_count, str(e))
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
            "workflow_id": self.state.workflow_id,
            "stage": self.state.stage.value,
            "error_message": self.state.error_message,
            "chunk_count": self.state.chunk_count,
            "records_ingested": self.state.records_ingested,
        }


@workflow.defn
class PromoteToProdWorkflow:
    """Promote a DEV-ingested document into the PROD Qdrant collection."""

    def __init__(self):
        self.state = None

    @workflow.run
    async def run(
        self,
        document_id: str,
        filename: str,
        original_workflow_id: str,
        page_count: int = 0,
        chunk_count: int = 0,
    ) -> dict:
        self.state = {
            "document_id": document_id,
            "filename": filename,
            "workflow_id": original_workflow_id,
            "stage": "ingesting_prod",
        }
        try:
            await _mirror_state(original_workflow_id, "ingesting_prod", page_count, chunk_count, None)
            result = await workflow.execute_activity(
                promote_document_to_prod_qdrant,
                args=[original_workflow_id, document_id, filename],
                start_to_close_timeout=timedelta(minutes=90),
                retry_policy=INGEST_RETRY,
            )

            if workflow.patched("publish-after-prod-v1"):
                # New: this workflow now also announces the network catalog,
                # since promotion just succeeded here (previously it never
                # published at all - see DocumentPipelineWorkflow/ReingestionWorkflow
                # for the equivalent post-promotion publish).
                self.state["stage"] = "publishing_to_network"
                await _mirror_state(original_workflow_id, "publishing_to_network", page_count, chunk_count, None)

                network_transaction_id = str(workflow.uuid4())
                await workflow.execute_activity(
                    publish_catalog_to_network,
                    args=[original_workflow_id, network_transaction_id],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=NETWORK_PUBLISH_RETRY,
                )

            await _mirror_state(original_workflow_id, "completed", page_count, chunk_count, None)
            self.state["stage"] = "completed"
            return {
                "document_id": document_id,
                "filename": filename,
                "stage": "completed",
                "records_promoted_to_prod": result.get("records_ingested", 0),
            }
        except Exception as e:
            self.state["stage"] = "failed"
            self.state["error_message"] = str(e)
            try:
                await _mirror_state(original_workflow_id, "failed", page_count, chunk_count, str(e))
            except Exception:
                pass
            raise

    @workflow.query
    def get_state(self) -> dict:
        return self.state or {}
