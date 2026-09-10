"""Tests for pipeline/workflows.py module-level configuration."""

import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from pipeline import workflows as workflows_module
from pipeline.workflows import (
    DocumentPipelineWorkflow,
    PromoteToProdWorkflow,
    ReingestionWorkflow,
    _network_publish_retry_policy,
)


class TestNetworkPublishRetryPolicy:
    @pytest.mark.unit
    def test_defaults_when_unset(self, monkeypatch):
        for var in (
            "DISCOVERY_SERVICE_PUBLISH_INITIAL_INTERVAL_SECONDS",
            "DISCOVERY_SERVICE_PUBLISH_BACKOFF_COEFFICIENT",
            "DISCOVERY_SERVICE_PUBLISH_MAX_INTERVAL_SECONDS",
            "DISCOVERY_SERVICE_PUBLISH_MAX_ATTEMPTS",
        ):
            monkeypatch.delenv(var, raising=False)

        policy = _network_publish_retry_policy()

        assert policy.initial_interval.total_seconds() == 30
        assert policy.backoff_coefficient == 2.0
        assert policy.maximum_interval.total_seconds() == 300
        assert policy.maximum_attempts == 5

    @pytest.mark.unit
    def test_reads_overrides_from_env(self, monkeypatch):
        monkeypatch.setenv("DISCOVERY_SERVICE_PUBLISH_INITIAL_INTERVAL_SECONDS", "10")
        monkeypatch.setenv("DISCOVERY_SERVICE_PUBLISH_BACKOFF_COEFFICIENT", "1.5")
        monkeypatch.setenv("DISCOVERY_SERVICE_PUBLISH_MAX_INTERVAL_SECONDS", "120")
        monkeypatch.setenv("DISCOVERY_SERVICE_PUBLISH_MAX_ATTEMPTS", "3")

        policy = _network_publish_retry_policy()

        assert policy.initial_interval.total_seconds() == 10
        assert policy.backoff_coefficient == 1.5
        assert policy.maximum_interval.total_seconds() == 120
        assert policy.maximum_attempts == 3


def _stub_activities(call_log: list):
    """Fake activities recording call order; registered on the test Worker and
    monkeypatched over the real ones referenced by pipeline.workflows."""

    @activity.defn(name="run_ocr_and_store")
    async def run_ocr_and_store(workflow_id: str, filepath: str) -> dict:
        call_log.append("run_ocr_and_store")
        return {"page_count": 1}

    @activity.defn(name="detect_and_translate_pages_from_db")
    async def detect_and_translate_pages_from_db(workflow_id: str) -> dict:
        call_log.append("detect_and_translate_pages_from_db")
        return {"page_count": 1, "translated_count": 0}

    @activity.defn(name="create_chunks_from_db")
    async def create_chunks_from_db(
        workflow_id: str, chunk_size: int, chunk_overlap: int, min_tokens: int
    ) -> dict:
        call_log.append("create_chunks_from_db")
        return {"chunk_count": 1}

    @activity.defn(name="auto_tag_chunks_from_db")
    async def auto_tag_chunks_from_db(workflow_id: str, filename: str = "") -> dict:
        call_log.append("auto_tag_chunks_from_db")
        return {}

    @activity.defn(name="ingest_document_from_db")
    async def ingest_document_from_db(
        workflow_id: str, document_id: str, filename: str, index_name: str = "documents-index"
    ) -> dict:
        call_log.append("ingest_document_from_db")
        return {"records_ingested": 1}

    @activity.defn(name="promote_document_to_prod_qdrant")
    async def promote_document_to_prod_qdrant(workflow_id: str, document_id: str, filename: str) -> dict:
        call_log.append("promote_document_to_prod_qdrant")
        return {"records_ingested": 1}

    @activity.defn(name="publish_catalog_to_network")
    async def publish_catalog_to_network(workflow_id: str, transaction_id: str) -> dict:
        call_log.append("publish_catalog_to_network")
        return {"status": "published"}

    @activity.defn(name="update_document_state")
    async def update_document_state(
        workflow_id: str,
        stage: str,
        page_count: int = 0,
        chunk_count: int = 0,
        error_message: str = None,
    ) -> dict:
        call_log.append(f"state:{stage}")
        return {}

    return {
        "run_ocr_and_store": run_ocr_and_store,
        "detect_and_translate_pages_from_db": detect_and_translate_pages_from_db,
        "create_chunks_from_db": create_chunks_from_db,
        "auto_tag_chunks_from_db": auto_tag_chunks_from_db,
        "ingest_document_from_db": ingest_document_from_db,
        "promote_document_to_prod_qdrant": promote_document_to_prod_qdrant,
        "publish_catalog_to_network": publish_catalog_to_network,
        "update_document_state": update_document_state,
    }


def _activity_calls(call_log: list) -> list:
    """call_log entries that aren't state-mirroring noise."""
    return [c for c in call_log if not c.startswith("state:")]


@pytest.mark.workflow
class TestPublishAfterProdOrdering:
    """publish_catalog_to_network must only run after a successful PROD
    promotion (or never, when PROD is disabled) - see docs/ADR/0004."""

    @pytest.mark.asyncio
    async def test_document_pipeline_publishes_after_prod_promotion(self, monkeypatch):
        call_log: list = []
        stubs = _stub_activities(call_log)
        for name, fn in stubs.items():
            monkeypatch.setattr(workflows_module, name, fn)

        task_queue = f"test-queue-{uuid.uuid4()}"
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[DocumentPipelineWorkflow],
                activities=list(stubs.values()),
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                handle = await env.client.start_workflow(
                    DocumentPipelineWorkflow.run,
                    args=["doc-1", "test.pdf", "/tmp/test.pdf", 450, 128, 100, "documents-index", True, False, False],
                    id=f"test-wf-{uuid.uuid4()}",
                    task_queue=task_queue,
                )
                await handle.signal(DocumentPipelineWorkflow.approve_prod)
                await handle.result()

        activities_called = _activity_calls(call_log)
        assert activities_called.count("publish_catalog_to_network") == 1
        assert activities_called.index("promote_document_to_prod_qdrant") < activities_called.index(
            "publish_catalog_to_network"
        )

    @pytest.mark.asyncio
    async def test_document_pipeline_skip_prod_never_publishes(self, monkeypatch):
        call_log: list = []
        stubs = _stub_activities(call_log)
        for name, fn in stubs.items():
            monkeypatch.setattr(workflows_module, name, fn)

        task_queue = f"test-queue-{uuid.uuid4()}"
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[DocumentPipelineWorkflow],
                activities=list(stubs.values()),
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                handle = await env.client.start_workflow(
                    DocumentPipelineWorkflow.run,
                    args=["doc-1", "test.pdf", "/tmp/test.pdf", 450, 128, 100, "documents-index", True, False, True],
                    id=f"test-wf-{uuid.uuid4()}",
                    task_queue=task_queue,
                )
                await handle.result()

        activities_called = _activity_calls(call_log)
        assert "publish_catalog_to_network" not in activities_called
        assert "promote_document_to_prod_qdrant" not in activities_called

    @pytest.mark.asyncio
    async def test_reingestion_workflow_does_not_publish(self, monkeypatch):
        call_log: list = []
        stubs = _stub_activities(call_log)
        for name, fn in stubs.items():
            monkeypatch.setattr(workflows_module, name, fn)

        task_queue = f"test-queue-{uuid.uuid4()}"
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[ReingestionWorkflow],
                activities=list(stubs.values()),
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                handle = await env.client.start_workflow(
                    ReingestionWorkflow.run,
                    args=["doc-1", "test.pdf", "orig-wf-1", 1, 1, "documents-index"],
                    id=f"test-wf-{uuid.uuid4()}",
                    task_queue=task_queue,
                )
                result = await handle.result()

        activities_called = _activity_calls(call_log)
        assert "ingest_document_from_db" in activities_called
        assert "publish_catalog_to_network" not in activities_called
        assert result["stage"] == "approval_for_prod"

    @pytest.mark.asyncio
    async def test_promote_to_prod_workflow_publishes_after_promotion(self, monkeypatch):
        call_log: list = []
        stubs = _stub_activities(call_log)
        for name, fn in stubs.items():
            monkeypatch.setattr(workflows_module, name, fn)

        task_queue = f"test-queue-{uuid.uuid4()}"
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[PromoteToProdWorkflow],
                activities=list(stubs.values()),
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                handle = await env.client.start_workflow(
                    PromoteToProdWorkflow.run,
                    args=["doc-1", "test.pdf", "orig-wf-1", 1, 1],
                    id=f"test-wf-{uuid.uuid4()}",
                    task_queue=task_queue,
                )
                await handle.result()

        activities_called = _activity_calls(call_log)
        assert activities_called == ["promote_document_to_prod_qdrant", "publish_catalog_to_network"]
