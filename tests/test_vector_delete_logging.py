import pytest

from pipeline import vector_store
from pipeline.api import api
from pipeline.vector_store import qdrant_store


def _store_down():
    raise ConnectionError("qdrant down")


def _client_down(**_kwargs):
    raise ConnectionError("prod qdrant down")


@pytest.fixture
def dev_store_down(monkeypatch):
    monkeypatch.setattr(vector_store, "get_vector_store", _store_down)


@pytest.fixture
def prod_store_down(monkeypatch):
    monkeypatch.setenv("PROD_VECTOR_DB_URL", "http://prod-qdrant:6333")
    monkeypatch.setattr(qdrant_store, "get_qdrant_client", _client_down)


def _only(caplog, message):
    [record] = [r for r in caplog.records if r.getMessage() == message]
    assert record.levelname == "ERROR"
    assert isinstance(record.exc_info[1], ConnectionError)
    return record


def test_chunk_delete_failure_logs_doc_and_chunk(dev_store_down, caplog):
    result = api.delete_single_chunk_from_index("doc-1", 7)

    assert result == {"deleted": False, "error": "qdrant down"}
    record = _only(caplog, "vector chunk delete failed")
    assert (record.doc_id, record.chunk) == ("doc-1", 7)


def test_document_delete_failure_logs_doc(dev_store_down, caplog):
    result = api.delete_chunks_from_index("doc-1")

    assert result == {"deleted": 0, "doc_id": "doc-1", "error": "qdrant down"}
    assert _only(caplog, "vector document delete failed").doc_id == "doc-1"


def test_prod_document_delete_failure_logs_doc_and_collection(prod_store_down, monkeypatch, caplog):
    monkeypatch.setenv("PROD_VECTOR_DB_COLLECTION_NAME", "prod-docs")

    result = api._delete_document_points_from_prod({"document_id": "doc-1"})

    assert result == {"deleted": False, "error": "prod qdrant down", "collection": "prod-docs"}
    record = _only(caplog, "prod vector delete failed")
    assert (record.doc_id, record.collection) == ("doc-1", "prod-docs")


def test_prod_scheme_delete_failure_logs_doc_and_collection(prod_store_down, monkeypatch, caplog):
    monkeypatch.delenv("PROD_SCHEME_VECTOR_DB_URL", raising=False)
    monkeypatch.delenv("PROD_SCHEME_VECTOR_DB_COLLECTION_NAME", raising=False)

    result = api._delete_scheme_points_from_prod({"document_id": "doc-1"})

    assert result == {"deleted": False, "error": "prod qdrant down", "collection": "schemes-index"}
    record = _only(caplog, "prod vector delete failed")
    assert (record.doc_id, record.collection) == ("doc-1", "schemes-index")
