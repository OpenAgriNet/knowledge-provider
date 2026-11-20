"""``POST /search`` with ``search_profile="advisory"`` through the real Qdrant engine.

Every advisory shares one text, so retrieval scores tie and the order comes
only from the BV provider's season, sowing-window and place boosts.
"""

import pytest
from qdrant_client import QdrantClient

from pipeline import db
from pipeline.search.passage_projection import _prepare_records
from pipeline.vector_store import qdrant_store
from tests.test_qdrant_validity import TODAY, clock_at
from tests.test_search_group_integration import VECTOR_SIZE, bag_of_words_vector

INDEX = "advisory-profile"
TEXT = "sorghum harvesting advisory"
PUNE = [{"codeScheme": "LGD", "areaCode": "27", "areaLevel": "state", "areaName": "Maharashtra"},
        {"codeScheme": "LGD", "areaCode": "519", "areaLevel": "district", "areaName": "Pune"}]
MAHARASHTRA = PUNE[:1]
ELSEWHERE = [{"codeScheme": "LGD", "areaCode": "9", "areaLevel": "state"},
             {"codeScheme": "LGD", "areaCode": "100", "areaLevel": "district"}]
WINDOW = "\n\nStart day relative to sowing: 105\n\nEnd day relative to sowing: 120"
ADVISORIES = [
    # doc, pop, season, areas, sowing window, crop, origin
    ("a-pune-rabi-window", 1, "Rabi", PUNE, True, "Sorghum", "webhook"),
    ("b-maharashtra-kharif", 2, "Kharif", MAHARASHTRA, False, "Sorghum", "webhook"),
    ("c-national-rabi", 3, "Rabi", [], False, "Sorghum", "document"),
    ("d-elsewhere-rabi-window", 4, "Rabi", ELSEWHERE, True, "Sorghum", "webhook"),
    ("e-rice", 5, "Rabi", PUNE, True, "Rice", "webhook"),
]


def advisory_points(doc, pop, season, areas, window, crop, origin):
    texts = [TEXT + (WINDOW if window else "")]
    if doc.startswith("a-"):
        texts.append("sorghum advisory second passage")  # same entry: must collapse to one hit
    db.upsert_document(doc, doc, f"{doc}.txt", f"blob://{doc}.txt", instance="bv", kind="advisory", code=doc, name=doc)
    db.save_chunks(doc, [{"chunk_number": n, "original_text": t} for n, t in enumerate(texts, start=1)])
    db.upsert_document_index_status(doc, INDEX, doc_id=doc, chunk_count_indexed=len(texts), status="indexed")
    metadata = {"subject_categories": ["Crop"], "seasons": [season], "coverage_areas": areas,
                "agriculture_subjects": [{"descriptor": {"name": crop}}],
                "subjects": [{"type": "crop", "descriptor": {"name": crop}}],
                "attributes": {"source_record": {"content_id": 1, "pop_id": pop}}}
    records = _prepare_records(doc, doc, [{"chunk_number": n, "original_text": t} for n, t in enumerate(texts, start=1)],
                               workflow_id=doc, instance="bv", kind="advisory", knowledge_metadata=metadata, source_type=origin)
    return [{**record, "doc_id": doc} for record in records]


@pytest.fixture
def advisory_store(monkeypatch, test_client):
    monkeypatch.setenv("EMBEDDING_VECTOR_SIZE", str(VECTOR_SIZE))
    monkeypatch.setenv("VECTOR_DB_COLLECTION_NAME", INDEX)
    monkeypatch.delenv("ADVISORY_DEFAULT_SEASON", raising=False)
    monkeypatch.setattr(qdrant_store, "embed_passages", lambda texts, **_: [bag_of_words_vector(t) for t in texts])
    monkeypatch.setattr(qdrant_store, "embed_query", lambda query, **_: bag_of_words_vector(query))
    store = qdrant_store.QdrantVectorStore(client=QdrantClient(":memory:"), clock=clock_at(TODAY))
    store.upsert(INDEX, [point for advisory in ADVISORIES for point in advisory_points(*advisory)])
    monkeypatch.setattr("pipeline.vector_store.get_vector_store", lambda: store)
    return store


def search(test_client, **fields):
    response = test_client.post("/search", json={"query": TEXT, "index_name": INDEX, "search_profile": "advisory", **fields})
    assert response.status_code == 200, response.text
    return response.json()


FARMER = {"crop": "jowar", "season": "Rabi", "days_after_sowing": 110, "state_lgd_code": 27, "district_lgd_code": 519}


@pytest.mark.integration
@pytest.mark.api
def test_season_crop_stage_and_place_order_the_answer(advisory_store, test_client):
    body = search(test_client, advisory=FARMER)
    # a: +0.2 place +0.15 window +0.1 season | d: window+season | b: statewide place | c: season only
    assert [hit["doc_id"] for hit in body["hits"]] == [
        "a-pune-rabi-window", "d-elsewhere-rabi-window", "b-maharashtra-kharif", "c-national-rabi"]
    assert [hit["location_match"] for hit in body["hits"]] == ["matched", "other", "matched", "national"]
    assert [hit["origin"] for hit in body["hits"]] == ["webhook", "webhook", "webhook", "docs-pipeline"]
    assert body["advisory_search"] == {
        "status": "success", "message": None, "crop": "Sorghum", "preferred_season": "Rabi",
        "location_status": "location_matched", "has_day_range": True, "days_matched": True, "has_location_specific": True,
    }
    assert body["effective_config"]["ranking_score_field"] == "_advisory_score"


@pytest.mark.integration
@pytest.mark.api
def test_one_passage_per_entry_and_bv_defaults(advisory_store, test_client):
    body = search(test_client)
    doc_ids = [hit["doc_id"] for hit in body["hits"]]
    assert len(doc_ids) == len(set(doc_ids)) == 5           # entry a's two passages collapsed to one
    config = body["effective_config"]
    assert (config["search_profile"], config["document_kind"], config["search_mode"]) == ("advisory", "advisory", "TENSOR")
    assert (config["top_k"], config["candidate_cap"], config["max_chunks_per_doc"]) == (5, 40, None)
    assert body["advisory_search"]["location_status"] == "not_requested"


@pytest.mark.integration
@pytest.mark.api
def test_crop_is_a_hard_filter_through_aliases(advisory_store, test_client):
    assert "e-rice" in {hit["doc_id"] for hit in search(test_client)["hits"]}
    assert "e-rice" not in {hit["doc_id"] for hit in search(test_client, advisory={"crop": "jowar"})["hits"]}
    rice = search(test_client, advisory={"crop": "dhan"})
    assert [hit["doc_id"] for hit in rice["hits"]] == ["e-rice"] and rice["advisory_search"]["crop"] == "Rice"
    assert search(test_client, advisory={"crop": "cotton"})["advisory_search"]["status"] == "not_found"


@pytest.mark.integration
@pytest.mark.api
def test_state_only_place_is_a_partial_match(advisory_store, test_client):
    body = search(test_client, advisory={"crop": "jowar", "state_lgd_code": 27})
    matches = {hit["doc_id"]: hit["location_match"] for hit in body["hits"]}
    assert matches["a-pune-rabi-window"] == "state_partial" and matches["b-maharashtra-kharif"] == "matched"
    assert body["hits"][0]["doc_id"] == "b-maharashtra-kharif"


def test_advisory_context_needs_the_advisory_profile(test_client):
    for body in ({"query": "x", "advisory": {"crop": "wheat"}},
                 {"query": "x", "search_profile": "advisory", "document_kind": "scheme"},
                 {"query": "x", "search_profile": "advisory", "advisory": {"days_after_sowing": "ten"}}):
        assert test_client.post("/search", json=body).status_code == 422
