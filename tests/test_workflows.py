"""Temporal workflow execution and replay checks."""

import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from pipeline.temporal.workflows import (
    DocumentPipelineWorkflow,
    PromoteToProdWorkflow,
    ReingestionWorkflow,
    document,
    policies,
    releases,
)
from pipeline.temporal.workflows.policies import _network_publish_retry_policy


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


def _stub_activities(call_log: list, source_first_flags: dict | None = None):
    """Fake activities recording call order; registered on the test Worker and
    monkeypatched over the real activities referenced by the workflow."""

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
        workflow_id: str, chunk_size: int, chunk_overlap: int, min_tokens: int, source_first: bool = False,
    ) -> dict:
        call_log.append("create_chunks_from_db")
        if source_first_flags is not None:
            source_first_flags["chunking"] = source_first
        return {"chunk_count": 1}

    @activity.defn(name="auto_tag_chunks_from_db")
    async def auto_tag_chunks_from_db(workflow_id: str, filename: str = "") -> dict:
        call_log.append("auto_tag_chunks_from_db")
        return {}

    @activity.defn(name="ingest_document_from_db")
    async def ingest_document_from_db(
        workflow_id: str, document_id: str, filename: str, index_name: str = "documents-index",
        batch_size: int = 10, release_network_id: str | None = None, source_first: bool = False,
        freeze_draft_revision: bool = False, revision_workflow_id: str | None = None,
    ) -> dict:
        call_log.append("ingest_document_from_db")
        if source_first_flags is not None:
            source_first_flags["indexing"] = source_first
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


def _install_stubs(monkeypatch, workflow_module, stubs):
    monkeypatch.setattr(policies, "update_document_state", stubs["update_document_state"])
    for name, fn in stubs.items():
        if hasattr(workflow_module, name):
            monkeypatch.setattr(workflow_module, name, fn)


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
        _install_stubs(monkeypatch, document, stubs)

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
    @pytest.mark.parametrize("filename,filepath,source_first,legacy", [
        ("test.pdf", "/tmp/test.pdf", False, False),
        ("notice.json", "/tmp/notice.json", True, False),
        ("source.txt", "intake://test", True, False),
        ("notice.json", "/tmp/notice.json", False, True),
    ])
    async def test_document_pipeline_skip_prod_never_publishes(self, monkeypatch, filename, filepath, source_first, legacy):
        call_log: list = []
        source_first_flags = {}
        stubs = _stub_activities(call_log, source_first_flags)
        _install_stubs(monkeypatch, document, stubs)

        task_queue = f"test-queue-{uuid.uuid4()}"
        with monkeypatch.context() as execution:
            if legacy:
                patched = document.workflow.patched
                execution.setattr(
                    document.workflow, "patched",
                    lambda name: False if name == "source-first-structured-draft-v1" else patched(name),
                )
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
                        args=["doc-1", filename, filepath, 450, 128, 100, "documents-index", True, False, True],
                        id=f"test-wf-{uuid.uuid4()}",
                        task_queue=task_queue,
                    )
                    await handle.result()
                    history = await handle.fetch_history()

        await Replayer(
            workflows=[DocumentPipelineWorkflow], workflow_runner=UnsandboxedWorkflowRunner(),
        ).replay_workflow(history)

        activities_called = _activity_calls(call_log)
        assert "publish_catalog_to_network" not in activities_called
        assert "promote_document_to_prod_qdrant" not in activities_called
        assert source_first_flags == {"chunking": source_first, "indexing": source_first}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("index_only", [False, True])
    async def test_reingestion_workflow_does_not_publish(self, monkeypatch, index_only):
        call_log: list = []
        stubs = _stub_activities(call_log)
        _install_stubs(monkeypatch, releases, stubs)

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
                    args=["doc-1", "test.pdf", "orig-wf-1", 1, 1, "documents-index", *([True] if index_only else [])],
                    id=f"test-wf-{uuid.uuid4()}",
                    task_queue=task_queue,
                )
                result = await handle.result()

        activities_called = _activity_calls(call_log)
        assert "ingest_document_from_db" in activities_called
        assert "publish_catalog_to_network" not in activities_called
        assert result["stage"] == ("completed" if index_only else "approval_for_prod")

    @pytest.mark.asyncio
    async def test_promote_to_prod_workflow_publishes_after_promotion(self, monkeypatch):
        call_log: list = []
        stubs = _stub_activities(call_log)
        _install_stubs(monkeypatch, releases, stubs)

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


@pytest.mark.workflow
@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_manual_review_has_no_chunk_review_pause(monkeypatch, legacy):
    """New documents go from chunking straight to the ingestion gate; histories
    recorded before skip-chunk-review-v1 keep the chunk review pause."""
    call_log: list = []
    stubs = _stub_activities(call_log)
    _install_stubs(monkeypatch, document, stubs)
    if legacy:
        patched = document.workflow.patched
        monkeypatch.setattr(
            document.workflow, "patched", lambda name: False if name == "skip-chunk-review-v1" else patched(name),
        )

    task_queue = f"test-queue-{uuid.uuid4()}"
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client, task_queue=task_queue, workflows=[DocumentPipelineWorkflow],
            activities=list(stubs.values()), workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            handle = await env.client.start_workflow(
                DocumentPipelineWorkflow.run,
                args=["doc-1", "test.pdf", "/tmp/test.pdf", 450, 128, 100, "documents-index", False, False, True],
                id=f"test-wf-{uuid.uuid4()}",
                task_queue=task_queue,
            )
            for signal in (DocumentPipelineWorkflow.approve_ocr, DocumentPipelineWorkflow.approve_translation,
                           DocumentPipelineWorkflow.approve_ingestion):
                await handle.signal(signal)
            if legacy:
                await handle.signal(DocumentPipelineWorkflow.approve_chunks)
            await handle.result()
            history = await handle.fetch_history()

    await Replayer(workflows=[DocumentPipelineWorkflow], workflow_runner=UnsandboxedWorkflowRunner()).replay_workflow(history)
    assert ("state:chunk_review" in call_log) is legacy
    assert "auto_tag_chunks_from_db" in call_log
    assert call_log.index("state:ready_for_ingestion") > call_log.index("auto_tag_chunks_from_db")
