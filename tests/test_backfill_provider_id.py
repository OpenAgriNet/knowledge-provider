import importlib.util
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from pipeline.network_provider import ProviderNaming

SCRIPT = Path(__file__).parents[1] / "scripts" / "backfill_provider_id.py"
COLLECTION = "documents-index"
NAMING = ProviderNaming(shared_instance="bv", shared_name="Bharat Vistaar", network_id="provider.oan.dev")


@pytest.fixture(scope="module")
def backfill_module():
    spec = importlib.util.spec_from_file_location("backfill_provider_id", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def target(backfill_module):
    client = QdrantClient(":memory:")
    client.create_collection(COLLECTION, vectors_config=qmodels.VectorParams(size=2, distance=qmodels.Distance.COSINE))
    points = [
        ("wf-mh-advisory", {"provider_id": "stale"}),
        ("wf-mh-advisory", {}),
        ("wf-bv-scheme", {}),
        ("wf-mh-document", {"provider_id": "stale"}),
        ("wf-bad", {}),
    ]
    client.upsert(
        COLLECTION,
        [
            qmodels.PointStruct(id=i, vector=[1.0, 0.0], payload={"workflow_id": workflow_id, **extra})
            for i, (workflow_id, extra) in enumerate(points, start=1)
        ],
    )
    return backfill_module.Target("dev", client, COLLECTION)


DOCUMENTS = [
    {"workflow_id": "wf-mh-advisory", "document_kind": "advisory", "instance": "mh"},
    {"workflow_id": "wf-bv-scheme", "document_kind": "scheme", "instance": "bv"},
    {"workflow_id": "wf-mh-document", "document_kind": "document", "instance": "mh"},
    {"workflow_id": "wf-bad", "document_kind": "advisory", "instance": "ladakh_ut"},
    {"workflow_id": "wf-not-indexed", "document_kind": "advisory", "instance": "ka"},
]


def provider_ids(client):
    points, _ = client.scroll(COLLECTION, limit=100, with_payload=True)
    return {point.id: point.payload.get("provider_id") for point in points}


@pytest.mark.unit
def test_plan_follows_the_publish_provider(backfill_module):
    planned = {p.workflow_id: p for p in backfill_module.plan(DOCUMENTS, NAMING)}

    assert planned["wf-mh-advisory"].provider_id == "mh.provider.oan.dev"
    assert planned["wf-bv-scheme"].provider_id == "bv.provider.oan.dev"
    assert planned["wf-mh-document"].provider_id is None
    assert planned["wf-bad"].error and planned["wf-bad"].provider_id is None


@pytest.mark.unit
def test_dry_run_counts_without_writing(backfill_module, target):
    before = provider_ids(target.client)
    planned = backfill_module.plan(DOCUMENTS, NAMING)

    stats = backfill_module.backfill(target, planned, apply=False)

    assert stats == {"documents": 3, "points_set": 3, "points_cleared": 1, "skipped_invalid": 1}
    assert provider_ids(target.client) == before


@pytest.mark.unit
def test_apply_sets_and_clears_by_workflow(backfill_module, target):
    planned = backfill_module.plan(DOCUMENTS, NAMING)

    backfill_module.backfill(target, planned, apply=True)

    assert provider_ids(target.client) == {
        1: "mh.provider.oan.dev",
        2: "mh.provider.oan.dev",
        3: "bv.provider.oan.dev",
        4: None,
        5: None,
    }


@pytest.mark.unit
def test_a_missing_collection_is_reported_and_skipped(backfill_module):
    target = backfill_module.Target("prod-schemes", QdrantClient(":memory:"), "schemes-index")

    stats = backfill_module.backfill(target, [], apply=True)

    assert stats["missing_collection"] is True


@pytest.mark.unit
def test_refuses_to_run_without_a_network_id(backfill_module, monkeypatch, capsys):
    monkeypatch.delenv("NETWORK_SENDER_ID", raising=False)
    monkeypatch.setattr(backfill_module, "targets_from_env", lambda: pytest.fail("must not touch Qdrant"))

    assert backfill_module.main(["--apply"]) == 1
    assert "NETWORK_SENDER_ID" in capsys.readouterr().out


@pytest.mark.unit
def test_dev_covers_the_default_and_every_indexed_collection(backfill_module, monkeypatch):
    monkeypatch.setenv("VECTOR_DB_COLLECTION_NAME", "documents-index")
    monkeypatch.setattr(
        backfill_module.db,
        "list_index_summaries",
        lambda **_: [{"index_name": "documents-index"}, {"index_name": "kharif-index"}, {"index_name": ""}],
    )

    assert backfill_module.dev_collections() == ["documents-index", "kharif-index"]


@pytest.mark.unit
@pytest.mark.parametrize("documents,exit_code", [(DOCUMENTS, 1), (DOCUMENTS[:3], 0)])
def test_exits_non_zero_when_a_document_is_skipped(backfill_module, monkeypatch, target, documents, exit_code):
    monkeypatch.setenv("NETWORK_SENDER_ID", "provider.oan.dev")
    monkeypatch.setattr(backfill_module, "all_documents", lambda: documents)
    monkeypatch.setattr(backfill_module, "targets_from_env", lambda: [target])

    assert backfill_module.main([]) == exit_code
