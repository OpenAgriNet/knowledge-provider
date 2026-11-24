import json

import httpx
import pytest
from qdrant_client import QdrantClient, models

from pipeline.db import metadata_review
from pipeline.metadata import extraction
from pipeline.search.passage_projection import (
    _document_fields_from_doc,
    _prepare_records,
    _revision_snapshot,
)
from pipeline.vector_store.qdrant_store import _build_filter, _record_payload

WHEAT = "This wheat guidance applies to Example District during Rabi."
RICE = "This section covers rice seed storage."


@pytest.fixture
def model(monkeypatch):
    calls = []
    real_client = httpx.AsyncClient

    def reply(request):
        calls.append(request.url.path)
        prompt = json.loads(request.content)["messages"][0]["content"]
        simulated_facts = []
        if WHEAT in prompt:
            simulated_facts.extend({"field": field, "value": value, "block": 1, "quote": WHEAT} for field, value in [
                ("category", "Crop"), ("crop", "Wheat"), ("season", "Rabi"), ("location", "Example District"),
            ])
        if RICE in prompt:
            simulated_facts.append({"field": "crop", "value": "Rice", "block": 2, "quote": RICE})
        simulated_facts.append({"field": "crop", "value": "Corn", "block": 1, "quote": "not in the source"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"facts": simulated_facts})}}]})

    monkeypatch.setenv("METADATA_BASE_URL", "http://model.invalid/v1")
    monkeypatch.setenv("METADATA_MODEL", "local-test")
    monkeypatch.setenv("METADATA_AREA_CODES", '{"Example District":{"codeScheme":"example","areaCode":"D1"}}')
    monkeypatch.setattr(extraction.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(reply), **kw))
    return calls


@pytest.mark.asyncio
async def test_extractor_checks_evidence_and_keeps_multiple_crops(model):
    proposal = await extraction.extract_metadata([(1, WHEAT), (2, RICE)])
    assert {f["value"] for f in proposal["facts"] if f["field"] == "crop"} == {"Wheat", "Rice"}
    assert all(f["evidence"]["end"] == len(f["evidence"]["quote"]) for f in proposal["facts"])
    assert model == ["/v1/chat/completions"]


@pytest.mark.asyncio
async def test_review_scope_round_trips_through_postgres_and_qdrant(db_connection, sample_document, model):
    wid = sample_document["workflow_id"]
    doc_id = db_connection.get_document(wid)["id"]
    chunks = [{"chunk_number": 1, "original_text": WHEAT}, {"chunk_number": 2, "original_text": RICE}]
    db_connection.save_chunks(wid, chunks)

    doc_review = await metadata_review.generate_review(doc_id, automatic=True)
    assert await metadata_review.generate_review(doc_id, automatic=True) == doc_review
    assert metadata_review.get_review(doc_id, 1) == {}
    pending = doc_review["pending"]
    metadata_review.accept_review(
        doc_id, wid, None, pending["id"], list(range(len(pending["facts"]))), actor_user_id="reviewer",
    )

    proposal = (await metadata_review.generate_review(doc_id, 1))["pending"]
    metadata_review.accept_review(
        doc_id, wid, 1, proposal["id"], list(range(len(proposal["facts"]))), actor_user_id="reviewer",
    )
    db_connection.save_chunks(wid, chunks)  # Exact passage recreation keeps accepted evidence.
    assert metadata_review.get_review(doc_id, 1)["accepted"]["id"] == proposal["id"]

    doc, pages, saved_chunks = db_connection.load_revision_material(wid)
    assert doc["reindex_required"]
    records = _prepare_records(doc["document_id"], "source.pdf", saved_chunks, pages=pages, **_document_fields_from_doc(doc))
    assert records[0]["document_scope"]["crop_keys"] == ["rice", "wheat"]
    assert records[0]["crop_keys"] == ["wheat"]
    assert records[1]["crop_keys"] == ["rice", "wheat"]
    assert "document_scope" not in records[1]
    assert all("metadata_document" not in row and "metadata_chunk" not in row for row in records)
    snapshot = _revision_snapshot(doc, pages, records, "review-index", None, chunks=saved_chunks)
    assert snapshot["annotations"]["chunks"]["1"]["facts"]
    assert "metadata_review" not in _record_payload(records[0])

    qdrant = QdrantClient(":memory:")
    qdrant.create_collection("review-test", vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
    qdrant.upsert("review-test", [
        models.PointStruct(id=number, vector=[1.0, 0.0], payload=_record_payload(record))
        for number, record in enumerate(records, 1)
    ])
    for scope, expected in (("document", [1, 2]), ("chunk", [1, 2])):
        filt = _build_filter(metadata_filters={"scope": scope, "crop": "wheat", "season": "rabi", "area": "example:d1"})
        hits, _ = qdrant.scroll("review-test", scroll_filter=filt)
        assert [hit.id for hit in hits] == expected

    new_proposal = (await metadata_review.generate_review(doc_id, 1))["pending"]
    db_connection.update_chunk(wid, 1, edited_text="The source was changed.")
    with pytest.raises(metadata_review.StaleMetadata):
        metadata_review.accept_review(doc_id, wid, 1, new_proposal["id"], [0], actor_user_id="reviewer")
    assert metadata_review.get_review(doc_id, 1) == {}


def test_generate_and_accept_chunk_through_console_api(test_client, db_connection, sample_document, model):
    wid = sample_document["workflow_id"]
    db_connection.save_chunks(wid, [{"chunk_number": 1, "original_text": WHEAT}, {"chunk_number": 2, "original_text": RICE}])
    path = f"/documents/{wid}/metadata-review"
    assert "pending" not in test_client.get(path).json()
    generated = test_client.post(path + "/generate?chunk_number=1")
    assert generated.status_code == 200, generated.text
    pending = generated.json()["pending"]
    accepted = test_client.post(path + "/accept?chunk_number=1", json={
        "proposal_id": pending["id"], "selected": [0, 1],
    })
    assert accepted.status_code == 200, accepted.text
    assert {fact["value"] for fact in accepted.json()["accepted"]["facts"]} == {"Crop", "Wheat"}
    assert "accepted" not in test_client.get(path + "?chunk_number=2").json()
    assert "accepted" not in test_client.get(path).json()


def test_manual_document_and_chunk_edits_override_and_restore_inheritance(test_client, db_connection, sample_document):
    wid = sample_document["workflow_id"]
    chunks = [{"chunk_number": 1, "original_text": WHEAT}, {"chunk_number": 2, "original_text": RICE}]
    db_connection.save_chunks(wid, chunks)
    path = f"/documents/{wid}/metadata-review"
    chunk_path = path + "?chunk_number=1"

    def save(url, overrides, facts):
        version = test_client.get(url).json()["version"]
        body = {"version": version, "overrides": overrides, "facts": facts}
        response = test_client.put(url, json=body)
        assert response.status_code == 200, response.text
        assert test_client.put(url, json=body).status_code == 409
        assert response.json()["accepted"]["facts"] == facts

    save(path, ["crop", "season", "location"], [
        {"field": "crop", "value": "Wheat"},
        {"field": "season", "value": "Rabi", "rule": 1},
        {"field": "location", "value": "D1", "area": {"codeScheme": "example", "areaCode": "D1"}, "rule": 1},
    ])
    save(chunk_path, ["crop", "location"], [
        {"field": "crop", "value": "Rice"},
        {"field": "location", "value": "D2", "area": {"codeScheme": "example", "areaCode": "D2"}, "rule": 1},
    ])

    def index_records():
        doc, pages, saved_chunks = db_connection.load_revision_material(wid)
        return _prepare_records(doc["document_id"], "source.pdf", saved_chunks, pages=pages, **_document_fields_from_doc(doc))

    first, second = index_records()
    assert first["document_scope"]["crop_keys"] == ["wheat"]
    assert first["crop_keys"] == ["rice"]
    assert first["applicability_groups"] == [{"coverage_area_keys": ["example:d2"], "season_keys": ["rabi"]}]
    assert "document_scope" not in second
    qdrant = QdrantClient(":memory:")
    qdrant.create_collection("overrides", vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
    qdrant.upsert("overrides", [
        models.PointStruct(id=number, vector=[1.0, 0.0], payload=_record_payload(record))
        for number, record in enumerate((first, second), 1)
    ])
    for scope, crop, area, expected in (
        ("document", "wheat", "example:d1", [1, 2]),
        ("chunk", "wheat", "example:d1", [2]),
        ("chunk", "rice", "example:d2", [1]),
    ):
        filt = _build_filter(metadata_filters={"scope": scope, "crop": crop, "season": "rabi", "area": area})
        hits, _ = qdrant.scroll("overrides", scroll_filter=filt)
        assert [hit.id for hit in hits] == expected

    save(chunk_path, ["crop"], [])
    assert "crop_keys" not in index_records()[0]
    save(chunk_path, [], [])
    assert index_records()[0]["crop_keys"] == ["wheat"]


def test_document_metadata_editor_saves_validity_and_keeps_it_at_document_scope(test_client, db_connection, sample_document):
    from pipeline.search.passage_projection import _validity_fields_from_doc

    wid = sample_document["workflow_id"]
    db_connection.save_chunks(wid, [{"chunk_number": 1, "original_text": WHEAT}, {"chunk_number": 2, "original_text": RICE}])
    path = f"/documents/{wid}/metadata-review"
    version = test_client.get(path).json()["version"]
    body = {"version": version, "overrides": ["crop"], "facts": [{"field": "crop", "value": "Wheat"}]}
    dates = {"startsAt": "2027-10-01", "endsAt": "2028-02-15"}
    rejected = test_client.put(path, json={**body, "validity": {**dates, "endsAt": "2027-09-01"}})
    assert rejected.status_code == 422
    response = test_client.put(path, json={**body, "validity": dates})
    assert response.status_code == 200, response.text
    validity = response.json()["validity"]
    assert validity["startsAt"].startswith("2027-10-01T00:00:00")
    assert validity["endsAt"].startswith("2028-02-15T23:59:59")
    assert response.json()["accepted"]["facts"] == body["facts"]
    assert test_client.get(path).json()["validity"] == validity

    chunk_path = path + "?chunk_number=1"
    chunk = test_client.get(chunk_path).json()
    rejected = test_client.put(chunk_path, json={**body, "version": chunk["version"], "validity": dates})
    assert rejected.status_code == 422

    def index_records():
        doc, pages, chunks = db_connection.load_revision_material(wid)
        return _prepare_records(
            doc["document_id"], "source.pdf", chunks, pages=pages,
            **_document_fields_from_doc(doc), **_validity_fields_from_doc(doc),
        )

    points = index_records()
    assert all(point["start_date"] == dates["startsAt"] and point["end_date"] == dates["endsAt"] for point in points)
    assert all(point["validity"] == validity for point in points)
    qdrant = QdrantClient(":memory:")
    qdrant.create_collection("validity", vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
    qdrant.upsert("validity", [
        models.PointStruct(id=number, vector=[1.0, 0.0], payload=_record_payload(record))
        for number, record in enumerate(points, 1)
    ])
    for date, count in (("2027-09-30", 0), ("2027-10-01", 2), ("2028-02-15", 2), ("2028-02-16", 0)):
        hits, _ = qdrant.scroll("validity", scroll_filter=_build_filter(valid_on=date))
        assert len(hits) == count

    cleared = test_client.put(path, json={
        **body, "version": response.json()["version"], "validity": None,
    })
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["validity"] is None
    assert all("start_date" not in point and "end_date" not in point for point in index_records())


def test_validity_end_only_preserves_an_explicit_timestamp(monkeypatch):
    from pipeline.documents.document_validity import TimePeriod, storage_value

    monkeypatch.setenv("KNOWLEDGE_TIMEZONE", "Asia/Kolkata")
    period = TimePeriod(endsAt="2028-02-14T20:00:00Z")
    assert storage_value(period) == {"endsAt": "2028-02-14T20:00:00Z"}
    assert period.calendar_days() == {"valid_from": None, "valid_to": "2028-02-15"}


def test_category_is_stored_once_and_extensions_stay_with_their_subject():
    from pipeline.search.passage_projection import migrate_passage_metadata

    records = _prepare_records("document", "source.txt", [{"chunk_number": 1, "original_text": WHEAT}],
        knowledge_metadata={
            "subject_categories": ["Crop"],
            "subjects": [
                {"type": "crop", "descriptor": {"name": "Wheat"}, "attributes": {"sowingDays": 14}},
                {"type": "crop", "descriptor": {"name": "Rice"}},
            ],
        },
    )
    point = _record_payload(records[0])
    assert point["subject_categories"] == ["Crop"]
    assert "document_scope" not in point
    assert all("attributes" not in subject for subject in point["subjects"])
    assert point["metadata"] == {"subjects": [
        {"type": "crop", "descriptor": {"name": "Wheat"}, "attributes": {"sowingDays": 14}},
    ]}

    migrated = migrate_passage_metadata({
        "subject_categories": ["Crop"],
        "metadata_document": {"categories": ["crop"]},
        "metadata_chunk": {"categories": ["livestock"]},
    })
    assert migrated["subject_categories"] == ["Livestock"]
    assert migrated["document_scope"]["subject_categories"] == ["Crop"]
    assert "metadata_chunk" not in migrated and "metadata_document" not in migrated
    qdrant = QdrantClient(":memory:")
    qdrant.create_collection("category", vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
    qdrant.upsert("category", [models.PointStruct(id=1, vector=[1.0, 0.0], payload=migrated)])
    for scope, category, expected in (("document", "Crop", 1), ("document", "Livestock", 0),
                                      ("chunk", "Crop", 0), ("chunk", "Livestock", 1)):
        filt = _build_filter(metadata_filters={"scope": scope, "category": category})
        assert len(qdrant.scroll("category", scroll_filter=filt)[0]) == expected


def test_frozen_review_can_clear_every_search_field():
    from pipeline.search.passage_projection import migrate_passage_metadata

    assert migrate_passage_metadata({"subject_categories": ["Crop"]}, reviewed=True) == {}
