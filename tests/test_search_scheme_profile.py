"""``POST /search`` with ``search_profile="scheme"`` through the real Qdrant engine.

Mirrors the BV provider's scheme search: the question names the scheme, the
asked section steers the embedding query, a scheme without indexed passages
widens to all schemes, and a scheme outside the index is answered with nothing.
"""

import pytest
from qdrant_client import QdrantClient

from pipeline import db
from pipeline.vector_store import qdrant_store
from tests.test_qdrant_validity import TODAY, clock_at
from tests.test_search_group_integration import VECTOR_SIZE, bag_of_words_vector

INDEX = "scheme-profile"
REGISTRY = [
    {"scheme_code": "pk", "scheme_name": "PM Kisan", "scheme_aliases": ["PM Kisan"]},
    {"scheme_code": "kcc", "scheme_name": "Kisan Credit Card", "scheme_aliases": ["KCC"]},
    # Listed in the catalog but with no indexed passages: catalog/index drift.
    {"scheme_code": "mif", "scheme_name": "Micro Irrigation Fund", "scheme_aliases": ["MIF"]},
]
PASSAGES = [
    ("pk-guideline", "pk", "PM Kisan", 1, "pm kisan eligibility land holding farmer families"),
    ("pk-guideline", "pk", "PM Kisan", 2, "pm kisan exclusion who cannot apply income tax payers"),
    ("pk-guideline", "pk", "PM Kisan", 3, "pm kisan benefit six thousand rupees per year"),
    ("kcc-guideline", "kcc", "Kisan Credit Card", 1, "kisan credit card eligibility farmers loan"),
    ("other-guideline", "other", "Uncatalogued Scheme", 1, "farmers eligibility subsidy loan kisan"),
]


def scheme_passage(doc_id, code, name, chunk, text):
    if chunk == 1:
        db.upsert_document(doc_id, doc_id, f"{doc_id}.txt", f"blob://{doc_id}.txt", instance="bv", kind="scheme", code=code, name=name)
        chunks = [{"chunk_number": n, "original_text": t} for d, _, _, n, t in PASSAGES if d == doc_id]
        db.save_chunks(doc_id, chunks)
        db.upsert_document_index_status(doc_id, INDEX, doc_id=doc_id, chunk_count_indexed=len(chunks), status="indexed")
    return {"doc_id": doc_id, "workflow_id": doc_id, "instance": "bv", "type": "scheme", "name": name,
            "document_identity": {"code": code, "name": name, "aliases": []},
            "chunk_num": chunk, "is_reference": False, "text": text}


@pytest.fixture
def scheme_store(monkeypatch, test_client):
    monkeypatch.setenv("EMBEDDING_VECTOR_SIZE", str(VECTOR_SIZE))
    monkeypatch.setenv("VECTOR_DB_COLLECTION_NAME", INDEX)
    monkeypatch.setattr(qdrant_store, "embed_passages", lambda texts, **_: [bag_of_words_vector(t) for t in texts])
    embedded: list[str] = []
    monkeypatch.setattr(qdrant_store, "embed_query", lambda query, **_: embedded.append(query) or bag_of_words_vector(query))
    store = qdrant_store.QdrantVectorStore(client=QdrantClient(":memory:"), clock=clock_at(TODAY))
    store.upsert(INDEX, [scheme_passage(*passage) for passage in PASSAGES])
    monkeypatch.setattr("pipeline.vector_store.get_vector_store", lambda: store)
    monkeypatch.setattr("pipeline.api.routers.search.load_scheme_registry", lambda **_: REGISTRY)
    store.embedded = embedded
    return store


def search(test_client, query, **fields):
    response = test_client.post("/search", json={"query": query, "index_name": INDEX, "search_profile": "scheme", **fields})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.integration
@pytest.mark.api
def test_question_names_the_scheme_and_answers_only_from_it(scheme_store, test_client):
    body = search(test_client, "PM Kisan overview")
    assert body["scheme_search"] == {
        "resolved_scheme_code": "pk", "scheme_code_source": "query", "effective_scheme_code": "pk",
        "widened_to_all_schemes": False, "query_intent": None, "section_focus": None,
        "supplemental_section": None, "supplemental_hits": 0, "status": "success", "message": None,
    }
    assert {hit["scheme_code"] for hit in body["hits"]} == {"pk"}
    # No per-document cap: all three PM Kisan passages come back (BV decision).
    assert body["final_count"] == 3
    config = body["effective_config"]
    assert (config["search_profile"], config["document_kind"], config["search_mode"]) == ("scheme", "scheme", "TENSOR")
    assert (config["top_k"], config["candidate_cap"], config["max_chunks_per_doc"]) == (10, 80, None)


@pytest.mark.integration
@pytest.mark.api
def test_asked_section_steers_the_embedding_query(scheme_store, test_client):
    body = search(test_client, "who is eligible for PM Kisan")
    assert (body["scheme_search"]["query_intent"], body["scheme_search"]["section_focus"]) == (
        "eligibility", "eligibility_with_exclusion")
    assert scheme_store.embedded[-1].startswith("who is eligible for PM Kisan farmer beneficiary selection criteria")


@pytest.mark.integration
@pytest.mark.api
def test_scheme_without_indexed_passages_widens_to_catalogued_schemes(scheme_store, test_client):
    body = search(test_client, "MIF eligibility for farmers")
    scheme = body["scheme_search"]
    assert (scheme["resolved_scheme_code"], scheme["effective_scheme_code"], scheme["widened_to_all_schemes"]) == ("mif", None, True)
    assert scheme["status"] == "success" and body["hits"]
    # Widened answers still come only from schemes in the catalog.
    assert {hit["scheme_code"] for hit in body["hits"]} <= {"pk", "kcc"}


@pytest.mark.integration
@pytest.mark.api
def test_unindexed_scheme_returns_nothing_without_searching(scheme_store, test_client):
    body = search(test_client, "Gobar Dhan eligibility")
    assert body["hits"] == [] and scheme_store.embedded == []
    assert (body["scheme_search"]["status"], body["scheme_search"]["message"]) == (
        "scheme_unavailable", "Scheme not available in the document index")


@pytest.mark.integration
@pytest.mark.api
def test_known_request_code_is_kept_and_unknown_code_falls_back_to_the_question(scheme_store, test_client):
    kept = search(test_client, "farmers loan eligibility", document_code="kcc")
    assert kept["scheme_search"]["scheme_code_source"] == "request"
    assert {hit["scheme_code"] for hit in kept["hits"]} == {"kcc"}
    fallback = search(test_client, "PM Kisan benefit", document_code="not-catalogued")
    assert (fallback["scheme_search"]["resolved_scheme_code"], fallback["scheme_search"]["scheme_code_source"]) == ("pk", "query")


@pytest.mark.integration
@pytest.mark.api
def test_default_profile_is_unchanged(scheme_store, test_client):
    response = test_client.post("/search", json={
        "query": "MIF eligibility for farmers", "index_name": INDEX, "document_kind": "scheme", "document_code": "mif",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    # Default search never widens an exact code and reports no scheme analysis.
    assert body["hits"] == [] and body["scheme_search"] is None
    assert body["effective_config"]["search_profile"] == "default"


def test_scheme_profile_rejects_other_kinds(test_client):
    response = test_client.post("/search", json={"query": "x", "search_profile": "scheme", "document_kind": "advisory"})
    assert response.status_code == 422


@pytest.mark.integration
@pytest.mark.api
def test_eligibility_answer_balances_eligibility_and_exclusion(scheme_store, test_client):
    body = search(test_client, "who is eligible for PM Kisan")
    assert [hit["section_type"] for hit in body["hits"]][:2] == ["eligibility", "exclusion"]
    assert body["effective_config"]["ranking_score_field"] == "_scheme_score"
    # The heading-title `section` used for citations is left untouched.
    assert all("section_type" in hit and hit.get("section") != hit["section_type"] for hit in body["hits"])


@pytest.mark.integration
@pytest.mark.api
def test_missing_section_triggers_a_second_search_with_its_vocabulary(scheme_store, test_client):
    body = search(test_client, "who is eligible for kisan credit card")
    assert body["scheme_search"]["resolved_scheme_code"] == "kcc"
    # KCC has no exclusion passage, so search ran again with exclusion words.
    assert body["scheme_search"]["supplemental_section"] == "exclusion"
    assert len(scheme_store.embedded) == 2
    assert scheme_store.embedded[1].endswith("scheme exclusion who cannot apply ineligible not eligible excluded")


@pytest.mark.integration
@pytest.mark.api
def test_explicit_per_document_cap_still_applies(scheme_store, test_client):
    assert search(test_client, "PM Kisan overview", max_chunks_per_doc=1)["final_count"] == 1


@pytest.mark.integration
@pytest.mark.api
def test_section_balancing_sees_candidates_beyond_top_k(scheme_store, test_client):
    # Regression: a per-document cap of top_k hid lower-ranked exclusion
    # passages of a single guideline from section balancing.
    body = search(test_client, "who is eligible for PM Kisan", top_k=2)
    assert body["effective_config"]["max_chunks_per_doc"] is None
    assert sorted(hit["section_type"] for hit in body["hits"]) == ["eligibility", "exclusion"]
