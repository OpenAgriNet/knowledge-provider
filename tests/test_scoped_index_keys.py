"""Regression checks for duplicate sources and legacy projection cleanup."""

import pytest

from pipeline.search.index_identity import projection_doc_id


@pytest.mark.unit
def test_same_source_crops_remain_independent_and_deleting_one_preserves_the_other(monkeypatch):
    from qdrant_client import QdrantClient
    from pipeline.search import passage_projection
    from pipeline.vector_store import qdrant_store

    monkeypatch.setattr(qdrant_store, "get_vector_size", lambda: 3)
    monkeypatch.setattr(qdrant_store, "embed_passages", lambda texts, **kw: [[1.0, 0.0, 0.0] for _ in texts])
    monkeypatch.setattr(qdrant_store, "embed_query", lambda *a, **kw: [1.0, 0.0, 0.0])
    documents = [
        {"id": f"00000000-0000-0000-0000-00000000000{n}", "document_id": "a" * 32}
        for n in (1, 2)
    ]
    records = [passage_projection._prepare_records(
        projection_doc_id(document), "same.pdf",
        [{"chunk_number": 1, "original_text": "Garden preparation guidance"}],
        workflow_id=f"workflow-{crop}", kind="advisory",
        crop_subjects=[{"subjectId": f"urn:crop:{crop}"}],
    )[0] for document, crop in zip(documents, ("cotton", "rice"))]
    assert records[0]["_id"] != records[1]["_id"]
    client = QdrantClient(location=":memory:")
    try:
        store = qdrant_store.QdrantVectorStore(client=client)
        store.upsert("duplicates", records)
        def search(crop):
            return store.search("duplicates", "garden", apply_validity=False,
                                crop_subject_ids=[f"urn:crop:{crop}"])["hits"]
        assert len(search("cotton")) == len(search("rice")) == 1
        store.delete_by_doc_id("duplicates", projection_doc_id(documents[0]))
        assert search("cotton") == []
        assert len(search("rice")) == 1
    finally:
        client.close()


@pytest.mark.db
def test_scoped_index_migration_retains_legacy_key_and_reupload_tracks_exact_key(db_connection):
    db = db_connection
    owner = db.upsert_document(
        workflow_id="scoped-document", document_id="a" * 32, filename="same.pdf",
        filepath="minio://documents/same.pdf", kind="advisory", code="scoped-document",
    )
    db.upsert_document_index_status("scoped-document", "documents-index", doc_id="a" * 32)
    document = db.get_document(owner)
    key = projection_doc_id(document)
    db.upsert_document_index_status("scoped-document", "documents-index", doc_id=key)
    assert db.get_document(owner)["superseded_doc_ids"] == {"dev": ["a" * 32]}
    assert db.get_document(key)["id"] == owner
    db.clear_superseded_doc_ids(owner, "dev", ["a" * 32])
    db.reset_document_for_reupload(owner, filename="same.pdf", filepath="minio://documents/same.pdf",
                                  fingerprint="a" * 32)
    assert db.get_document(owner)["superseded_doc_ids"] == {}
    db.reset_document_for_reupload(owner, filename="changed.pdf", filepath="minio://documents/changed.pdf",
                                  fingerprint="b" * 32)
    assert db.get_document(owner)["superseded_doc_ids"] == {"dev": [key]}


@pytest.mark.db
def test_api_cleanup_uses_recorded_key_after_source_replacement(db_connection):
    from pipeline.api.routers.document_lifecycle import _vector_doc_ids

    db = db_connection
    owner = db.upsert_document(
        workflow_id="scoped-cleanup", document_id="a" * 32, filename="same.pdf",
        filepath="minio://documents/same.pdf", kind="advisory", code="scoped-cleanup",
    )
    key = projection_doc_id(db.get_document(owner))
    db.upsert_document_index_status(owner, "documents-index", doc_id=key)
    db.reset_document_for_reupload(owner, filename="changed.pdf", filepath="minio://documents/changed.pdf",
                                  fingerprint="b" * 32)
    assert _vector_doc_ids(db.get_document(owner), "dev") == [key]


def test_index_only_cleanup_targets_the_serving_collection(monkeypatch):
    from pipeline.api import index_helpers

    calls = []

    class Store:
        def delete_by_doc_id(self, collection, document):
            calls.append((collection, document))
            return {"deleted": 1}

        def delete_chunk(self, collection, document, chunk):
            calls.append((collection, document, chunk))
            return {"deleted": 1}

    monkeypatch.setenv("VECTOR_DB_COLLECTION_NAME", "new-serving")
    monkeypatch.setattr("pipeline.vector_store.get_vector_store", lambda: Store())
    index_helpers.delete_chunks_from_index("doc-1")
    index_helpers.delete_single_chunk_from_index("doc-1", 2)
    assert calls == [("new-serving", "doc-1"), ("new-serving", "doc-1", 2)]
