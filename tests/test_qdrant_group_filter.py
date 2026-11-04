"""Unit tests for the group (``instance``) filter in the Qdrant store.

Same two styles as test_qdrant_validity.py: filter-shape tests pin the
condition the store builds, fake-client tests pin that every search mode
actually hands it to Qdrant.
"""

import pytest
from qdrant_client.http import models as qmodels

from pipeline.vector_store.qdrant_store import QdrantVectorStore, _build_filter
from tests.test_qdrant_validity import TODAY, FakeClient, clock_at


def match_values_in(filt, key):
    """The exact-match values the filter requires for payload ``key``."""
    found = []
    for condition in filt.must or []:
        if getattr(condition, "key", None) != key:
            continue
        match = getattr(condition, "match", None)
        value = getattr(match, "value", None)
        if value is not None:
            found.append(value)
    return found


class TestBuildFilterGroup:
    @pytest.mark.unit
    def test_adds_an_exact_match_on_instance(self):
        filt = _build_filter(instance="mh")

        assert match_values_in(filt, "instance") == ["mh"]

    @pytest.mark.unit
    def test_leaves_instance_out_when_none_is_given(self):
        assert _build_filter() is None
        assert match_values_in(_build_filter(exclude_reference=True), "instance") == []

    @pytest.mark.unit
    def test_composes_with_reference_exclusion_and_validity(self):
        filt = _build_filter(instance="mh", exclude_reference=True, valid_on=TODAY)

        keys = [getattr(c, "key", None) for c in filt.must]
        assert "instance" in keys
        assert "is_reference" in keys
        # validity contributes two nested should-clauses with no top-level key
        assert keys.count(None) == 2

    @pytest.mark.unit
    def test_instance_is_a_plain_keyword_match(self):
        filt = _build_filter(instance="mh")

        (condition,) = filt.must
        assert isinstance(condition, qmodels.FieldCondition)
        assert isinstance(condition.match, qmodels.MatchValue)


class TestSearchAppliesGroup:
    @pytest.mark.unit
    def test_lexical_mode_passes_the_instance_to_scroll(self):
        client = FakeClient()
        store = QdrantVectorStore(client=client, clock=clock_at(TODAY))

        store.search("idx", "kisan", search_mode="LEXICAL", instance="mh")

        assert match_values_in(client.scroll_filter, "instance") == ["mh"]

    @pytest.mark.unit
    def test_tensor_mode_passes_the_instance_to_query_points(self, monkeypatch):
        monkeypatch.setattr(
            "pipeline.vector_store.qdrant_store.embed_query", lambda *a, **k: [0.0, 1.0]
        )
        client = FakeClient()
        store = QdrantVectorStore(client=client, clock=clock_at(TODAY))

        store.search("idx", "kisan", search_mode="TENSOR", instance="mh")

        assert match_values_in(client.query_filter, "instance") == ["mh"]

    @pytest.mark.unit
    def test_no_instance_means_no_instance_condition(self):
        client = FakeClient()
        store = QdrantVectorStore(client=client, clock=clock_at(TODAY))

        store.search("idx", "kisan", search_mode="LEXICAL")

        assert match_values_in(client.scroll_filter, "instance") == []
