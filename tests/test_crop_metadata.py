"""Crop context is supplied explicitly, persisted, and retained by retrieval."""

import json
from types import SimpleNamespace

import pytest

from pipeline.documents.models import DocumentMetadata, DocumentSummary
from pipeline.metadata.crop_metadata import parse_crop_subject_ids, parse_crop_subjects

CROPS = [{
    "subjectId": "https://taxonomy.openagrinet.global/crops/cotton",
    "descriptor": {"code": "COTTON", "name": "Cotton"},
}]


@pytest.mark.unit
@pytest.mark.parametrize("raw", [
    "not-json", "{}", "null", "[{}]", '[{"subjectId":"cotton"}]',
    '[{"descriptor":{}}]', '[{"descriptor":{"name":" "}}]',
    '[{"descriptor":{"name":"Cotton"},"unknown":true}]',
    json.dumps(CROPS * 2),
    json.dumps(CROPS * 33),
    json.dumps([{"subjectId": "https://example.org/cotton field"}]),
    json.dumps([{"subjectId": "https:\\example.org\\cotton"}]),
])
def test_rejects_invalid_crop_context(raw):
    with pytest.raises(ValueError):
        parse_crop_subjects(raw)


@pytest.mark.unit
def test_optional_crop_context_and_schema_identifiers():
    assert parse_crop_subjects(None) is None
    assert parse_crop_subjects("[]") == []
    assert parse_crop_subjects(json.dumps(CROPS)) == CROPS
    assert parse_crop_subjects('[{"subjectId":"urn:crop:cotton"}]') == [{"subjectId": "urn:crop:cotton"}]
    assert parse_crop_subjects('[{"descriptor":{"name":" Cotton "}}]') == [{"descriptor": {"name": "Cotton"}}]
    assert DocumentMetadata().crop_subjects == []
    response = DocumentSummary(
        document_id="doc", workflow_id="wf", filename="cotton.pdf",
        stage="registered", page_count=0, chunk_count=0,
        crop_subjects=[{"descriptor": {"name": "Cotton"}}],
    ).model_dump()
    assert response["crop_subjects"] == [{"descriptor": {"name": "Cotton"}}]


@pytest.mark.unit
def test_crop_context_survives_chunk_payload_and_hit_projection():
    from pipeline.search import passage_projection
    from pipeline.vector_store.qdrant_store import _hit_from_point, _record_payload

    record, = passage_projection._prepare_records(
        "doc", "cotton.pdf", [{"chunk_number": 1, "original_text": "Irrigate cotton carefully."}],
        workflow_id="wf",
        **passage_projection._document_fields_from_doc({"kind": "advisory", "crop_subjects": CROPS}),
    )
    payload = _record_payload(record)
    assert payload["subjects"] == [{**crop, "type": "crop"} for crop in CROPS]
    assert "crop_subjects" not in payload
    hit = _hit_from_point(SimpleNamespace(payload=payload, id=record["_id"], score=1.0))
    assert hit["crop_subjects"] == CROPS


@pytest.mark.unit
@pytest.mark.parametrize("value", ["cotton", ["cotton"], [None], [123], ["https://example.org/cotton"] * 33])
def test_rejects_invalid_crop_filter(value):
    with pytest.raises(ValueError):
        parse_crop_subject_ids(value)


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["TENSOR", "LEXICAL"])
def test_real_local_qdrant_matches_crop_and_excludes_other_crops(monkeypatch, mode):
    from qdrant_client import QdrantClient

    from pipeline.vector_store import qdrant_store

    monkeypatch.setattr(qdrant_store, "get_vector_size", lambda: 3)
    monkeypatch.setattr(qdrant_store, "embed_passages", lambda texts, **kw: [[1.0, 0.0, 0.0] for _ in texts])
    monkeypatch.setattr(qdrant_store, "embed_query", lambda *a, **kw: [1.0, 0.0, 0.0])
    client = QdrantClient(location=":memory:")
    try:
        store = qdrant_store.QdrantVectorStore(client=client)
        store.upsert("crops", [{
            "_id": "a" * 32, "text": "cotton irrigation", "crop_subjects": CROPS,
            "provider_id": "provider-a", "is_reference": False,
        }])
        common = {"name": "crops", "query": "irrigation", "search_mode": mode, "apply_validity": False}
        hit, = store.search(**common, crop_subject_ids=[CROPS[0]["subjectId"]])["hits"]
        assert hit["crop_subjects"] == CROPS
        assert store.search(**common, crop_subject_ids=["https://taxonomy.openagrinet.global/crops/rice"])["hits"] == []
        assert store.search(**common, crop_subject_ids=[CROPS[0]["subjectId"]], provider_id="provider-b")["hits"] == []
        assert len(store.search(**common)["hits"]) == 1
        assert len(store.search(**common, crop_subject_ids=[])["hits"]) == 1
    finally:
        client.close()


@pytest.mark.unit
@pytest.mark.parametrize("damaged_field", ["missing", "payload", "vector"])
def test_release_upsert_verifies_the_readable_point(monkeypatch, damaged_field):
    from qdrant_client import QdrantClient

    from pipeline.vector_store import qdrant_store

    monkeypatch.setattr(qdrant_store, "get_vector_size", lambda: 3)
    monkeypatch.setattr(qdrant_store, "embed_passages", lambda texts, **kw: [[1.0, 0.0, 0.0] for _ in texts])
    client = QdrantClient(location=":memory:")
    try:
        store = qdrant_store.QdrantVectorStore(client=client)
        record = {"_id": "a" * 32, "text": "cotton irrigation", "release_network_id": "network.example"}
        assert store.upsert("release", [record])["records_ingested"] == 1
        retrieve = client.retrieve

        def read_damaged_point(**kwargs):
            if damaged_field == "missing":
                return []
            point, = retrieve(**kwargs)
            if damaged_field == "payload":
                point.payload = {"text": "unrelated content"}
            else:
                point.vector = [0.0, 0.0, 1.0]
            return [point]

        monkeypatch.setattr(client, "retrieve", read_damaged_point)
        with pytest.raises(ValueError, match="could not verify"):
            store.upsert("release", [record])
    finally:
        client.close()


@pytest.mark.db
@pytest.mark.api
def test_upload_persists_crop_context_and_search_returns_it(
    test_client, db_connection, sample_pdf_content, capturing_store, monkeypatch,
):
    from pipeline.api import api
    from pipeline.search import passage_projection
    from pipeline.vector_store.qdrant_store import _hit_from_point, _record_payload

    api.limiter.reset()
    response = test_client.post(
        "/upload", params={"kind": "advisory", "code": "crop-context"},
        files={"file": ("cotton.pdf", sample_pdf_content, "application/pdf")},
        data={"crop_subjects": json.dumps(CROPS)},
    )
    assert response.status_code == 200, response.text
    assert response.json()["crop_subjects"] == CROPS
    workflow_id = response.json()["workflow_id"]
    document = db_connection.get_document(workflow_id)
    assert document["crop_subjects"] == CROPS
    with db_connection.get_connection() as conn:
        row = conn.execute("SELECT metadata FROM documents WHERE id = %s::uuid", (document["id"],)).fetchone()
    assert row["metadata"]["crop_subjects"] == CROPS
    assert test_client.get(f"/documents/{workflow_id}").json()["crop_subjects"] == CROPS
    assert test_client.get("/documents").json()[0]["crop_subjects"] == CROPS

    record, = passage_projection._prepare_records(
        document["document_id"], document["filename"],
        [{"chunk_number": 1, "original_text": "Cotton irrigation guidance."}],
        workflow_id=workflow_id, **passage_projection._document_fields_from_doc(document),
    )
    capturing_store.hits = [_hit_from_point(SimpleNamespace(
        id=record["_id"], score=1.0, payload=_record_payload(record),
    ))]
    result = test_client.post("/search", json={
        "query": "cotton irrigation", "search_mode": "TENSOR", "crop_subject_ids": [CROPS[0]["subjectId"]],
    })
    assert result.status_code == 200, result.text
    assert result.json()["hits"][0]["crop_subjects"] == CROPS
    assert capturing_store.captured["crop_subject_ids"] == [CROPS[0]["subjectId"]]
    assert test_client.post("/search", json={"query": "cotton", "crop_subject_ids": ["cotton"]}).status_code == 400


@pytest.mark.db
def test_reupload_preserves_omitted_crop_context_and_can_explicitly_clear_it(db_connection):
    document_id = db_connection.upsert_document(
        workflow_id="crop-reupload", document_id="a" * 32, filename="cotton.pdf",
        filepath="minio://documents/cotton.pdf", kind="advisory", code="cotton",
        crop_subjects=CROPS,
    )
    db_connection.reset_document_for_reupload(
        document_id, filename="cotton-v2.pdf", filepath="minio://documents/cotton-v2.pdf",
        fingerprint="b" * 32,
    )
    assert db_connection.get_document(document_id)["crop_subjects"] == CROPS
    db_connection.reset_document_for_reupload(
        document_id, filename="general.pdf", filepath="minio://documents/general.pdf",
        fingerprint="c" * 32, crop_subjects=[],
    )
    assert db_connection.get_document(document_id)["crop_subjects"] == []


@pytest.mark.db
@pytest.mark.api
def test_invalid_crop_metadata_has_no_upload_or_workflow_side_effects(
    test_client, sample_pdf_content, mock_object_store, mock_temporal_client,
):
    from pipeline.api import api

    api.limiter.reset()
    response = test_client.post(
        "/upload", params={"kind": "advisory", "code": "invalid-crop"},
        files={"file": ("cotton.pdf", sample_pdf_content, "application/pdf")},
        data={"crop_subjects": "[{}]"},
    )
    assert response.status_code == 422
    mock_object_store.put.assert_not_called()
    mock_temporal_client.start_workflow.assert_not_called()
