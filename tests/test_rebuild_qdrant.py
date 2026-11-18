from uuid import uuid4

import pytest
from qdrant_client import QdrantClient

from pipeline.vector_store.qdrant_store import QdrantVectorStore
from tests.migration_kit import load_migration_tool


@pytest.mark.db
def test_rebuild_uses_completed_snapshot_and_never_reuses_collection(db_connection, monkeypatch):
    from pipeline.vector_store import qdrant_store
    rebuild = load_migration_tool("rebuild_qdrant_from_postgres")

    db = db_connection
    workflow = "frozen-projection"
    doc_id = db.upsert_document(
        workflow_id=workflow, document_id="a" * 32, filename="source.pdf",
        filepath="minio://docs/source.pdf", instance="bv", kind="advisory", code="frozen-projection",
    )
    accepted_id = str(uuid4())
    db.save_draft_revision_snapshot(doc_id, workflow, accepted_id, {"source": {"source_file_fingerprint": "a" * 32}, "passages": [{
        "_id": "a" * 32, "doc_id": doc_id, "chunk_num": 0, "text": "accepted passage", "workflow_id": workflow,
    }]})
    job = db.get_latest_document_job(workflow)
    db.update_document_job(job["id"], status="completed")
    db.create_document_job(doc_id, "pipeline", temporal_workflow_id="pending-projection")
    db.save_draft_revision_snapshot(doc_id, "pending-projection", str(uuid4()), {"passages": [{
        "_id": "b" * 32, "doc_id": doc_id, "text": "unfinished changes", "workflow_id": workflow,
    }]})
    client = QdrantClient(":memory:")
    store = QdrantVectorStore(client=client)
    monkeypatch.setenv("VECTOR_DB_COLLECTION_NAME", "serving")
    monkeypatch.setattr(rebuild, "QdrantVectorStore", lambda: store)
    monkeypatch.setattr(qdrant_store, "get_vector_size", lambda: 2)
    def embed(texts, *, batch_size, **_):
        assert batch_size == 4
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(qdrant_store, "embed_passages", embed)
    assert rebuild.rebuild("replacement", embedding_batch_size=4)["indexed_passages"] == 1
    points, _ = client.scroll("replacement")
    assert [point.payload["text"] for point in points] == ["accepted passage"]
    assert db.visible_release_hits([point.payload for point in points]) == [point.payload for point in points]
    from pipeline.search.index_identity import projection_doc_id

    assert points[0].payload["doc_id"] == projection_doc_id(db.get_document(doc_id))
    import asyncio

    from pipeline.api.routers.search import run_search
    from pipeline.auth.models import AuthUser
    from pipeline.search.models import SearchRequest

    monkeypatch.setenv("VECTOR_DB_COLLECTION_NAME", "replacement")
    monkeypatch.setattr("pipeline.vector_store.get_vector_store", lambda: store)
    monkeypatch.setattr(qdrant_store, "embed_query", lambda *_args, **_kw: [1.0, 0.0])
    response = asyncio.run(run_search(SearchRequest(query="accepted passage", include_expired=True,
                                      exclude_reference=False), AuthUser("restore-reader", instances=["bv"])))
    assert [hit["text"] for hit in response["hits"]] == ["accepted passage"]
    monkeypatch.setenv("VECTOR_DB_COLLECTION_NAME", "serving")
    with pytest.raises(ValueError, match="already exists"):
        rebuild.rebuild("replacement")
    with pytest.raises(ValueError, match="serving collection"):
        rebuild.rebuild("serving")
    assert client.count("replacement", exact=True).count == 1


def test_removes_only_stale_passages_for_the_reindexed_document(monkeypatch):
    from pipeline.vector_store import qdrant_store

    store = QdrantVectorStore(client=QdrantClient(":memory:"))
    monkeypatch.setattr(qdrant_store, "get_vector_size", lambda: 2)
    monkeypatch.setattr(qdrant_store, "embed_passages", lambda texts, **_: [[1.0, 0.0] for _ in texts])
    original = [
        {"_id": "a" * 32, "doc_id": "first", "text": "obsolete"},
        {"_id": "b" * 32, "doc_id": "second", "text": "unrelated"},
    ]
    updated = [{"_id": "c" * 32, "doc_id": "first", "text": "edited"}]
    store.upsert("index", original)
    store.upsert("index", updated)
    store.remove_superseded_passages("index", "first", updated)
    store.remove_superseded_passages("index", "first", updated)
    points, _ = store.client.scroll("index")
    assert {point.payload["text"] for point in points} == {"edited", "unrelated"}
