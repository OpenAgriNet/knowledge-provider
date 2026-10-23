import pytest
from qdrant_client import QdrantClient

from pipeline.vector_store import qdrant_store
from tests.test_qdrant_validity import TODAY, clock_at
from tests.test_search_group_integration import VECTOR_SIZE, bag_of_words_vector

INDEX = "provider-integration"

CHUNKS = [
    {"doc_id": "mh-advisory", "instance": "mh", "provider_id": "advisory.mh.provider.oan.dev",
     "text": "cotton pest advisory for kharif"},
    {"doc_id": "mh-scheme", "instance": "mh", "provider_id": "scheme.mh.provider.oan.dev",
     "text": "cotton pest subsidy scheme"},
    {"doc_id": "bv-advisory", "instance": "bv", "provider_id": "advisory.bv.provider.oan.dev",
     "text": "cotton pest advisory nationwide"},
    {"doc_id": "mh-document", "instance": "mh", "text": "cotton pest field notes"},
]


@pytest.fixture
def local_store(monkeypatch, test_client):
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
@pytest.mark.parametrize("mode", ["HYBRID", "TENSOR", "LEXICAL"])
def test_one_provider_limits_hits_to_its_chunks(local_store, test_client, mode):
    body = search(test_client, provider_id="advisory.mh.provider.oan.dev", search_mode=mode)

    assert body["hits"], "the mh advisory chunk must be found"
    assert {hit["doc_id"] for hit in body["hits"]} == {"mh-advisory"}
    assert body["effective_config"]["provider_id"] == ["advisory.mh.provider.oan.dev"]


@pytest.mark.integration
@pytest.mark.api
def test_a_list_of_providers_matches_any_of_them(local_store, test_client):
    body = search(test_client, provider_id=["advisory.mh.provider.oan.dev", "scheme.mh.provider.oan.dev"])

    assert {hit["doc_id"] for hit in body["hits"]} == {"mh-advisory", "mh-scheme"}


@pytest.mark.integration
@pytest.mark.api
def test_no_provider_searches_every_chunk(local_store, test_client):
    body = search(test_client)

    assert {hit["doc_id"] for hit in body["hits"]} == {chunk["doc_id"] for chunk in CHUNKS}


@pytest.mark.integration
@pytest.mark.api
def test_provider_composes_with_group(local_store, test_client):
    body = search(test_client, provider_id="advisory.bv.provider.oan.dev", group="mh")

    assert body["hits"] == []
