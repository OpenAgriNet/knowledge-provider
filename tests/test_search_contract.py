"""Search identity, controls and the authenticated network serving boundary."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from qdrant_client import QdrantClient, models

from pipeline.auth.models import AuthUser
from pipeline.auth.permissions import Permission
from pipeline.search.access import network_search_collection
from pipeline.search.models import SearchRequest
from pipeline.vector_store.qdrant_store import _build_filter


@pytest.mark.parametrize("field,value", [
    ("top_k", "ten"), ("top_k", True), ("top_k", 0), ("top_k", 51),
    ("candidate_cap", []), ("candidate_multiplier", -1), ("max_chunks_per_doc", 0),
    ("search_mode", []), ("search_mode", "missing"), ("hybrid_alpha", float("nan")),
    ("use_e5_prefix", "false"), ("include_expired", "false"), ("ef_search", 0),
])
def test_controls_are_typed_and_bounded(field, value):
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": "PM Kisan overview", field: value})


def test_published_request_schema_matches_the_runtime_model():
    from pathlib import Path

    import yaml

    from scripts.export_search_contract import request_components

    published = yaml.safe_load((Path(__file__).resolve().parents[1] / "docs/openapi-search.yaml").read_text())
    for name, schema in request_components().items():
        assert published["components"]["schemas"][name] == schema


def test_exact_code_requires_kind_and_network_cannot_disable_validity():
    for fields in ({"document_code": "pk"}, {"network_id": "a", "include_expired": True},
                   {"network_id": "a", "valid_on": "2020-01-01"}, {"network_id": "a", "include_raw_hits": True}):
        with pytest.raises(ValidationError):
            SearchRequest(query="PM Kisan overview", **fields)


def test_exact_filters_run_inside_qdrant_and_never_broaden_an_empty_match():
    client = QdrantClient(":memory:")
    client.create_collection("search", vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
    payloads = [
        {"type": "scheme", "document_identity": {"code": "pk"}, "release_network_id": "a"},
        {"type": "scheme", "document_identity": {"code": "kcc"}, "release_network_id": "a"},
        {"type": "advisory", "document_identity": {"code": "pk"}, "release_network_id": "a"},
        {"type": "scheme", "document_identity": {"code": "pk"}, "release_network_id": "b"},
        {"type": "scheme", "document_identity": {"code": "pk"}},
        {"type": "scheme", "scheme_code": "pk", "release_network_id": "a"},
        {"type": "scheme", "document_identity": {"code": "kcc"}, "scheme_code": "pk", "release_network_id": "a"},
    ]
    client.upsert("search", [models.PointStruct(id=i, vector=[1.0, 0.0], payload=p) for i, p in enumerate(payloads)])
    points, _ = client.scroll("search", scroll_filter=_build_filter(document_kind="scheme", document_code="pk", release_network_id="a"))
    assert {p.id for p in points} == {0, 5}
    points, _ = client.scroll("search", scroll_filter=_build_filter(document_kind="scheme", document_code="missing", release_network_id="a"))
    assert points == []
    client.close()


def test_network_target_requires_verified_explicit_grant(monkeypatch):
    monkeypatch.delenv("KP_INDEX_ONLY", raising=False)
    monkeypatch.setenv("SEARCH_NETWORK_PRINCIPALS", '{"adapter":["a"]}')
    monkeypatch.setattr("pipeline.search.access.managed_release_targets", lambda _: {"a": "released-a", "b": "released-b"})
    user = AuthUser("adapter")
    assert network_search_collection(user, "a", "scheme", None) == "released-a"
    for actor, network, collection, status in (
        (user, "b", None, 403), (AuthUser("other"), "a", None, 403),
        (AuthUser("adapter", token_disabled_mode=True), "a", None, 403), (user, "a", "draft", 422),
    ):
        with pytest.raises(HTTPException) as error:
            network_search_collection(actor, network, "scheme", collection)
        assert error.value.status_code == status
    monkeypatch.setenv("KP_INDEX_ONLY", "true")
    with pytest.raises(HTTPException):
        network_search_collection(user, "a", "scheme", None)


def released_document(db, network="a"):
    document = db.upsert_document("release-test", "a" * 32, "scheme.txt", "blob://docs/scheme.txt", kind="scheme", code="pk", name="PM Kisan")
    job = db.get_latest_document_job(document)
    db.update_document_job(job["id"], config_json={"release_network_id": network})
    revision = str(uuid4())
    snapshot = {"metadata": {"kind": "scheme", "code": "pk", "name": "Released PM Kisan", "aliases": ["PM Kisan"]},
                "passages": [{"text": "Released passage"}]}
    db.save_revision_snapshot(document, "release-test", network, revision, snapshot, collection="released-a")
    db.activate_network_release(document, "release-test", network, str(uuid4()))
    return document, revision


def test_network_liveness_rejects_drafts_wrong_scope_withdrawn_and_disabled(db_connection):
    db = db_connection
    document, revision = released_document(db)
    hit = {"revision_id": revision, "release_network_id": "a", "text": "Released passage"}
    drafts = [{"text": "draft"}, {**hit, "release_network_id": "b"}, {**hit, "revision_id": str(uuid4())}]
    assert db.visible_release_hits([*drafts, hit], network_id="a") == [hit]
    assert db.visible_release_hits([hit], network_id="b") == []
    db.set_document_disabled(document, True)
    assert db.visible_release_hits([hit], network_id="a") == []
    db.set_document_disabled(document, False)
    db.withdraw_network_release(document, "a", revision)
    assert db.visible_release_hits([hit], network_id="a") == []


def test_network_catalog_keeps_released_labels_when_a_new_draft_changes(db_connection):
    db = db_connection
    document, revision = released_document(db)
    db.update_document_fields(document, name="Unapproved title", code="unapproved", stage="chunk_review")
    entry, = db.list_catalog_entries(network_id="a")
    assert (entry["code"], entry["name"], entry["revision_id"]) == ("pk", "Released PM Kisan", revision)
    assert db.list_catalog_entries(network_id="b") == []
    assert db.list_catalog_entries(status="live") == []


def test_collection_state_does_not_collide_across_network_targets(db_connection):
    db = db_connection
    document = db.upsert_document("collection-state", "b" * 32, "file.txt", "blob://docs/file.txt", kind="advisory", code="test")
    for collection in ("released-a", "released-b", "draft"):
        db.upsert_document_index_status(document, collection, doc_id=f"owner:{collection}")
    assert set(db.get_document(document)["index_status"]) == {"released-a", "released-b", "draft"}
    assert db.get_document_index_status(document, "released-a")["doc_id"] == "owner:released-a"


def test_configured_kind_uses_one_schema_map_for_intake_and_catalog(db_connection, monkeypatch):
    from pipeline.documents.document_kinds import validate_document_kind

    monkeypatch.setenv("DOCUMENT_KINDS", "resource")
    with pytest.raises(ValueError, match="mapping"):
        validate_document_kind("resource")
    monkeypatch.setenv("DOCUMENT_KIND_SCHEMAS", '{"resource":"example:KnowledgeResource"}')
    assert validate_document_kind("resource") == "resource"
    document = db_connection.upsert_document("kind-resource", "c" * 32, "resource.txt", "blob://docs/resource.txt", kind="resource", code="resource")
    assert db_connection.get_document(document)["catalog_id"]
    assert db_connection.get_catalog_entry("resource", kind="resource")["type"] == "example:KnowledgeResource"


@pytest.fixture
def network_reader(monkeypatch):
    from pipeline.api import api
    from pipeline.api.routers import search
    from pipeline.auth import deps

    monkeypatch.delenv("KP_INDEX_ONLY", raising=False)
    monkeypatch.setenv("SEARCH_NETWORK_PRINCIPALS", '{"adapter":["a"]}')
    monkeypatch.setattr("pipeline.search.access.managed_release_targets", lambda _: {"a": "released-a", "b": "released-b"})
    monkeypatch.setattr(search, "managed_release_collections", lambda: {"released-a": "a", "released-b": "b"})
    monkeypatch.setattr(search, "managed_release_targets", lambda _: {"a": "released-a", "b": "released-b"})
    user = AuthUser("adapter", permissions={Permission.SEARCH}, instances=["bv"])
    monkeypatch.setitem(api.app.dependency_overrides, deps.get_current_user, lambda: user)
    return user


@pytest.mark.parametrize("fields,status", [({}, 422), ({"network_id": "b"}, 403),
    ({"network_id": "a", "index_name": "draft"}, 422), ({"network_id": "a", "include_expired": True}, 422)])
def test_network_route_denies_scope_bypass_before_retrieval(test_client, capturing_store, network_reader, fields, status):
    response = test_client.post("/search", json={"query": "PM Kisan overview", **fields})
    assert response.status_code == status, response.text
    assert capturing_store.captured == {}


@pytest.mark.parametrize(("display_name", "expected"), [("Bharat Vistaar", "Bharat Vistaar"), (None, "BV")])
def test_network_route_passes_exact_identity_and_instance_boundary(
    test_client, capturing_store, network_reader, db_connection, display_name, expected,
):
    document, revision = released_document(db_connection)
    with db_connection.get_connection() as conn:
        conn.execute(
            "UPDATE instances SET display_name = %s WHERE id = (SELECT provider_id FROM documents WHERE id = %s)",
            (display_name, document),
        )
    hit = {"doc_id": "released", "instance": "bv", "name": "PM Kisan", "text": "Exclusion criteria", "_score": .8337,
           "revision_id": revision, "release_network_id": "a"}
    capturing_store.hits = [{"text": "unmarked draft"}, {**hit, "release_network_id": "b"}, hit]
    response = test_client.post("/search", json={"query": "PM Kisan overview", "network_id": "a", "document_kind": "scheme",
        "document_code": "PK", "top_k": 10, "candidate_cap": 80, "max_chunks_per_doc": 10, "search_mode": "TENSOR"})
    assert response.status_code == 200, response.text
    assert len(response.json()["hits"]) == 1
    assert {key: response.json()["hits"][0][key] for key in hit} == hit
    assert response.json()["hits"][0]["instance_name"] == expected
    assert capturing_store.captured["document_code"] == "pk"
    assert capturing_store.captured["document_kind"] == "scheme"
    assert capturing_store.captured["release_network_id"] == "a"
    assert capturing_store.captured["allowed_instances"] == ["bv"]
    assert capturing_store.captured["name"] == "released-a"
    assert response.json()["effective_config"]["ranking_score_field"] == "_score"


def test_adapter_collection_inventory_is_scoped_and_empty_grants_cannot_fall_back(test_client, capturing_store, network_reader, monkeypatch):
    assert {row["network_id"] for row in test_client.get("/search/collections").json()} == {"a"}
    monkeypatch.setenv("SEARCH_NETWORK_PRINCIPALS", '{"adapter":[]}')
    assert test_client.get("/search/collections").json() == []
    assert test_client.post("/search", json={"query": "PM Kisan overview"}).status_code == 422
    assert capturing_store.captured == {}


def test_chunk_export_uses_canonical_identity_title_and_metadata(test_client, db_connection, sample_document):
    from pipeline.search.index_identity import projection_doc_id
    from pipeline.search.passage_projection import (
        _document_fields_from_doc,
        _prepare_records,
        _validity_fields_from_doc,
    )

    wid = sample_document["workflow_id"]
    db_connection.update_document_fields(wid, name="Reviewed title", knowledge_metadata={"subject_categories": ["Crop"], "tags": ["scouting"]})
    db_connection.save_chunks(wid, [{"chunk_number": 1, "original_text": "Wheat scouting advice", "page_start": 1, "page_end": 1}])
    document = db_connection.get_document(wid)
    chunks = db_connection.get_chunks(wid)
    canonical = _prepare_records(projection_doc_id(document), document["filename"], chunks, workflow_id=wid,
        instance=document["instance"], pages=[], source_first=True, **_document_fields_from_doc(document), **_validity_fields_from_doc(document))
    response = test_client.get(f"/documents/{wid}/export/chunks")
    assert response.status_code == 200, response.text
    assert response.json() == canonical
