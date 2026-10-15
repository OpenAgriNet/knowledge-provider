"""Integration: ``POST /search`` with ``group`` through the real Qdrant engine.

qdrant-client ships an in-process mode (``QdrantClient(":memory:")``) that
runs the real filtering and vector engine, so this exercises the route, the
store and the engine together without Docker. Only the embedding model is
faked: search quality is not under test here, the group filter is.
"""

import zlib

import pytest
from qdrant_client import QdrantClient

from pipeline.vector_store import qdrant_store
from tests.test_qdrant_validity import TODAY, clock_at

INDEX = "group-integration"
VECTOR_SIZE = 8

CHUNKS = [
    {"doc_id": "mh-current", "instance": "mh", "text": "cotton pest advisory for kharif"},
    {"doc_id": "mh-expired", "instance": "mh", "text": "cotton pest advisory last season",
     "start_date": "2025-01-01", "end_date": "2025-12-31"},
    {"doc_id": "ka-current", "instance": "ka", "text": "cotton pest advisory for karnataka"},
    {"doc_id": "bv-current", "instance": "bv", "text": "cotton pest advisory nationwide"},
]


def bag_of_words_vector(text: str) -> list[float]:
    """Deterministic stand-in for the embedding model: one slot per hashed token.

    crc32, not ``hash()``: Python salts str hashes per process, which would make
    the slots differ between runs."""
    vector = [0.01] * VECTOR_SIZE
    for token in text.lower().split():
        vector[zlib.crc32(token.encode()) % VECTOR_SIZE] += 1.0
    return vector


@pytest.fixture
def local_store(monkeypatch, test_client):
    """A real QdrantVectorStore over an in-memory engine, loaded with CHUNKS and
    wired in as the API's vector store."""
    monkeypatch.setenv("EMBEDDING_VECTOR_SIZE", str(VECTOR_SIZE))
    monkeypatch.setattr(qdrant_store, "embed_passages", lambda texts, **_: [bag_of_words_vector(t) for t in texts])
    monkeypatch.setattr(qdrant_store, "embed_query", lambda query, **_: bag_of_words_vector(query))

    store = qdrant_store.QdrantVectorStore(client=QdrantClient(":memory:"), clock=clock_at(TODAY))
    store.upsert(INDEX, [{"chunk_num": 1, "is_reference": False, **chunk} for chunk in CHUNKS])
    monkeypatch.setattr("pipeline.vector_store.get_vector_store", lambda: store)
    return store


def search(test_client, **fields):
    body = {"query": "cotton pest", "index_name": INDEX, "top_k": 10, "max_chunks_per_doc": 5, **fields}
    response = test_client.post("/search", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.integration
@pytest.mark.api
def test_every_chunk_is_indexed(local_store):
    assert local_store.client.count(INDEX, exact=True).count == len(CHUNKS)


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.parametrize("mode", ["HYBRID", "TENSOR", "LEXICAL"])
def test_group_limits_hits_to_that_group_in_every_mode(local_store, test_client, mode):
    body = search(test_client, group="mh", search_mode=mode)

    assert body["effective_config"]["group"] == "mh"
    assert body["hits"], "the mh chunk must be found"
    assert {hit["instance"] for hit in body["hits"]} == {"mh"}


@pytest.mark.integration
@pytest.mark.api
def test_no_group_searches_every_group(local_store, test_client):
    body = search(test_client)

    assert body["effective_config"]["group"] is None
    assert {hit["instance"] for hit in body["hits"]} == {"mh", "ka", "bv"}


@pytest.mark.integration
@pytest.mark.api
def test_unknown_group_is_an_empty_success(local_store, test_client):
    body = search(test_client, group="xx")

    assert body["hits"] == []
    assert body["final_count"] == 0


@pytest.mark.integration
@pytest.mark.api
def test_group_and_validity_compose_in_the_engine(local_store, test_client):
    current_only = search(test_client, group="mh")
    with_expired = search(test_client, group="mh", include_expired=True)

    assert {hit["doc_id"] for hit in current_only["hits"]} == {"mh-current"}
    assert {hit["doc_id"] for hit in with_expired["hits"]} == {"mh-current", "mh-expired"}
