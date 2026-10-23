import pytest
from qdrant_client.http import models as qmodels

from pipeline.vector_store.qdrant_store import PAYLOAD_FIELDS, QdrantVectorStore, _build_filter
from tests.test_qdrant_validity import TODAY, FakeClient, clock_at


def provider_ids_in(filt):
    for condition in (filt.must if filt else None) or []:
        if getattr(condition, "key", None) == "provider_id":
            return list(condition.match.any)
    return []


class TestProviderIdPayload:
    @pytest.mark.unit
    def test_is_a_declared_payload_field(self):
        assert "provider_id" in PAYLOAD_FIELDS

    @pytest.mark.unit
    def test_is_indexed_as_a_keyword(self):
        assert QdrantVectorStore.PAYLOAD_INDEXES["provider_id"] == qmodels.PayloadSchemaType.KEYWORD


class TestBuildFilterProviderId:
    @pytest.mark.unit
    def test_matches_any_of_the_given_ids(self):
        filt = _build_filter(provider_ids=["advisory.mh.provider.oan.dev", "scheme.mh.provider.oan.dev"])

        (condition,) = filt.must
        assert isinstance(condition.match, qmodels.MatchAny)
        assert provider_ids_in(filt) == ["advisory.mh.provider.oan.dev", "scheme.mh.provider.oan.dev"]

    @pytest.mark.unit
    @pytest.mark.parametrize("provider_ids", [None, []])
    def test_leaves_provider_out_when_none_is_given(self, provider_ids):
        assert _build_filter(provider_ids=provider_ids) is None

    @pytest.mark.unit
    def test_composes_with_group_and_validity(self):
        filt = _build_filter(provider_ids=["advisory.mh.provider.oan.dev"], instance="mh", valid_on=TODAY)

        keys = [getattr(c, "key", None) for c in filt.must]
        assert {"provider_id", "instance"} <= set(keys)
        assert keys.count(None) == 2


class TestSearchAppliesProviderId:
    @pytest.mark.unit
    def test_lexical_mode_passes_it_to_scroll(self):
        client = FakeClient()
        store = QdrantVectorStore(client=client, clock=clock_at(TODAY))

        store.search("idx", "kisan", search_mode="LEXICAL", provider_ids=["advisory.mh.provider.oan.dev"])

        assert provider_ids_in(client.scroll_filter) == ["advisory.mh.provider.oan.dev"]

    @pytest.mark.unit
    def test_tensor_mode_passes_it_to_query_points(self, monkeypatch):
        monkeypatch.setattr("pipeline.vector_store.qdrant_store.embed_query", lambda *a, **k: [0.0, 1.0])
        client = FakeClient()
        store = QdrantVectorStore(client=client, clock=clock_at(TODAY))

        store.search("idx", "kisan", search_mode="TENSOR", provider_ids=["scheme.bv.provider.oan.dev"])

        assert provider_ids_in(client.query_filter) == ["scheme.bv.provider.oan.dev"]
