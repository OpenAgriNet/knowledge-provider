import pytest
from qdrant_client.http import models as qmodels

from pipeline.vector_store.qdrant_store import PAYLOAD_FIELDS, QdrantVectorStore, _build_filter
from tests.test_qdrant_validity import TODAY, FakeClient, clock_at


def provider_id_in(filt):
    for condition in (filt.must if filt else None) or []:
        if getattr(condition, "key", None) == "provider_id":
            return condition.match.value
    return None


class TestProviderIdPayload:
    @pytest.mark.unit
    def test_is_a_declared_payload_field(self):
        assert "provider_id" in PAYLOAD_FIELDS

    @pytest.mark.unit
    def test_is_indexed_as_a_keyword(self):
        assert QdrantVectorStore.PAYLOAD_INDEXES["provider_id"] == qmodels.PayloadSchemaType.KEYWORD


class TestBuildFilterProviderId:
    @pytest.mark.unit
    def test_matches_the_given_id_exactly(self):
        filt = _build_filter(provider_id="mh.provider.oan.dev")

        (condition,) = filt.must
        assert isinstance(condition.match, qmodels.MatchValue)
        assert provider_id_in(filt) == "mh.provider.oan.dev"

    @pytest.mark.unit
    def test_leaves_provider_out_when_none_is_given(self):
        assert _build_filter(provider_id=None) is None

    @pytest.mark.unit
    def test_composes_with_group_and_validity(self):
        filt = _build_filter(provider_id="mh.provider.oan.dev", instance="mh", valid_on=TODAY)

        keys = [getattr(c, "key", None) for c in filt.must]
        assert {"provider_id", "instance"} <= set(keys)
        assert keys.count(None) == 2


class TestSearchAppliesProviderId:
    @pytest.mark.unit
    def test_lexical_mode_passes_it_to_scroll(self):
        client = FakeClient()
        store = QdrantVectorStore(client=client, clock=clock_at(TODAY))

        store.search("idx", "kisan", search_mode="LEXICAL", provider_id="mh.provider.oan.dev")

        assert provider_id_in(client.scroll_filter) == "mh.provider.oan.dev"

    @pytest.mark.unit
    def test_tensor_mode_passes_it_to_query_points(self, monkeypatch):
        monkeypatch.setattr("pipeline.vector_store.qdrant_store.embed_query", lambda *a, **k: [0.0, 1.0])
        client = FakeClient()
        store = QdrantVectorStore(client=client, clock=clock_at(TODAY))

        store.search("idx", "kisan", search_mode="TENSOR", provider_id="bv.provider.oan.dev")

        assert provider_id_in(client.query_filter) == "bv.provider.oan.dev"
