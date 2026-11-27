from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from pipeline import db, vector_store
from pipeline.api import api
from pipeline.auth.deps import get_current_user
from pipeline.auth.jwt import claims_to_user


@pytest.fixture
def provider_client(monkeypatch):
    actor = claims_to_user({"sub": "rice-member", "groups": ["/states/RICE/contributor"]})
    monkeypatch.setitem(api.app.dependency_overrides, get_current_user, lambda: actor)

    @asynccontextmanager
    async def no_services(_app):
        yield

    monkeypatch.setattr(api.app.router, "lifespan_context", no_services)
    with TestClient(api.app) as client:
        yield client


def test_blob_registration_resolves_the_source_only_within_the_provider(provider_client, monkeypatch):
    uri = "minio://documents/registered-advisory.pdf"
    unknown_uri = "minio://documents/unassigned-advisory.pdf"
    lookup = Mock(side_effect=lambda source, provider: "a" * 32 if (source, provider) == (uri, "rice") else None)
    monkeypatch.setattr(db, "owned_source_fingerprint", lookup)
    monkeypatch.setattr(db, "list_used_scheme_codes", lambda **_: set())
    monkeypatch.setattr(db, "get_document", lambda _: None)
    monkeypatch.setattr(db, "upsert_document", Mock(return_value="document-id"))
    monkeypatch.setattr(db, "create_document_job", Mock())
    monkeypatch.setattr(api, "_log_audit", Mock())
    start = AsyncMock()
    monkeypatch.setattr(api, "temporal_client", SimpleNamespace(start_workflow=start))

    unassigned = provider_client.post("/documents?kind=advisory", json={"filepath": unknown_uri})
    local_file = provider_client.post("/documents?kind=advisory", json={"filepath": "/data/documents/advisory.pdf"})
    local_batch = provider_client.post("/documents/batch?kind=advisory", json={"directory": "/data/documents"})
    assert [unassigned.status_code, local_file.status_code, local_batch.status_code] == [404, 403, 405]
    start.assert_not_called()

    assigned = provider_client.post("/documents?kind=advisory", json={"filepath": uri})
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["source_file_fingerprint"] == "a" * 32
    assert start.call_args.kwargs["args"][2] == uri
    assert [call.args for call in lookup.call_args_list] == [(unknown_uri, "rice"), (uri, "rice")]


def test_vector_diagnostics_return_only_attributed_points_without_writing(provider_client, monkeypatch):
    legacy_key = "a" * 32
    doc = {
        "id": "6138e9a2-d87d-4ffb-9fa6-135d710b5c85", "workflow_id": "rice-document", "instance": "rice",
        "index_status": {"prod": {"index_name": "published-advisories", "doc_id": legacy_key}},
    }
    monkeypatch.setattr(db, "get_document", lambda _: doc)
    monkeypatch.setattr(db, "get_chunks", lambda *args, **kwargs: [{}])
    write = Mock()
    monkeypatch.setattr(db, "upsert_document_index_status", write)
    hits = [
        {"instance": "rice", "workflow_id": "rice-document", "text": "owned passage", "text_for_embedding": "private diagnostic"},
        {"instance": "rice", "workflow_id": "other-rice-document", "text": "other document"},
        {"instance": "cotton", "workflow_id": "other-document", "text": "other provider"},
        {"text": "unattributed passage"},
    ]
    list_points = Mock(return_value=[{"doc_id": legacy_key, **hit} for hit in hits])
    monkeypatch.setattr(vector_store, "get_vector_store", lambda: SimpleNamespace(backend="qdrant", list_by_doc_id=list_points))

    response = provider_client.get("/documents/rice-document/qdrant")
    assert response.status_code == 200, response.text
    assert [hit["text"] for hit in response.json()["hits"]] == ["owned passage"]
    assert "text_for_embedding" not in response.json()["hits"][0]
    assert provider_client.get("/documents/rice-document/qdrant?index_name=another-index").status_code == 404
    list_points.assert_called_once_with("published-advisories", legacy_key, limit=1000)
    write.assert_not_called()


def test_index_only_diagnostics_ignore_retired_collection_on_restore(provider_client, monkeypatch):
    monkeypatch.setenv("KP_INDEX_ONLY", "true")
    monkeypatch.setattr(vector_store, "get_default_index_name", lambda: "documents-rebuilt")
    doc_id = "6138e9a2-d87d-4ffb-9fa6-135d710b5c85"
    fingerprint = "b" * 32
    monkeypatch.setattr(db, "get_document", lambda _: {
        "id": doc_id, "workflow_id": "rice-document", "instance": "rice",
        "document_id": fingerprint,
        "index_status": {"dev": {"index_name": "documents-index", "doc_id": "legacy-id"}},
    })
    monkeypatch.setattr(db, "get_chunks", lambda *args, **kwargs: [{}])
    list_points = Mock(return_value=[])
    monkeypatch.setattr(vector_store, "get_vector_store", lambda: SimpleNamespace(backend="qdrant", list_by_doc_id=list_points))

    response = provider_client.get("/documents/rice-document/qdrant")
    chunk_response = provider_client.get("/documents/rice-document/qdrant/chunks?chunk_number=1")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "missing"
    assert chunk_response.status_code == 403
    assert [call.args[:2] for call in list_points.call_args_list] == [
        ("documents-rebuilt", f"document:{doc_id}:{fingerprint}"),
    ]


def test_provider_index_summary_uses_scoped_counts(provider_client, monkeypatch):
    counts = Mock(return_value=[{"index_name": "published-advisories", "documents": 1}])
    monkeypatch.setattr(db, "list_index_summaries", counts)
    global_counts = Mock()
    monkeypatch.setattr(vector_store, "get_vector_store", lambda: SimpleNamespace(backend="qdrant", get_stats=global_counts))

    response = provider_client.get("/indexes/summary")
    assert response.status_code == 200, response.text
    assert response.json()[0]["documents"] == 1
    assert response.json()[0]["live_stats"] is None
    assert provider_client.get("/indexes/published-advisories/stats").status_code == 403
    assert provider_client.get("/indexes/published-advisories/settings").status_code == 403
    counts.assert_called_once_with(include_disabled=None, instances=["rice"])
    global_counts.assert_not_called()
