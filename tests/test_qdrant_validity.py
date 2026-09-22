"""Unit tests for validity filtering in the Qdrant store.

Two styles on purpose: the filter-shape tests pin the Qdrant conditions the
store builds (no server needed), and the fake-client tests pin that search
actually applies them and honours the injected clock.
"""

from datetime import datetime

import pytest
from qdrant_client.http import models as qmodels

from pipeline.vector_store.qdrant_store import (
    PAYLOAD_FIELDS,
    QdrantVectorStore,
    _build_filter,
    _record_payload,
    _validity_conditions,
)


def clock_at(stamp: str):
    return lambda: datetime.strptime(stamp, "%Y-%m-%d")


TODAY = "2026-09-22"


class FakeClient:
    """Records the filter it was asked to search with."""

    def __init__(self):
        self.query_filter = None
        self.scroll_filter = None

    def get_collection(self, name):
        raise RuntimeError("collection missing")

    def query_points(self, collection_name, query, query_filter, limit, with_payload, search_params=None):
        self.query_filter = query_filter
        return type("Result", (), {"points": []})()

    def scroll(self, collection_name, scroll_filter=None, limit=None, offset=None, with_payload=True, with_vectors=False):
        self.scroll_filter = scroll_filter
        return [], None


def dates_in(filt):
    """The (key, operator, day) triples the filter compares dates with.

    The day is normalised back to `YYYY-MM-DD`: qdrant-client's DatetimeRange
    widens the date we pass into a datetime, which is the wire form and not
    what these tests are about.
    """
    found = []
    for condition in filt.must or []:
        for branch in getattr(condition, "should", None) or []:
            rng = getattr(branch, "range", None)
            if rng is None:
                continue
            for op in ("lte", "gte", "lt", "gt"):
                value = getattr(rng, op, None)
                if value is not None:
                    day = getattr(value, "date", lambda: value)()
                    found.append((branch.key, op, str(day)))
    return found


def empty_keys_in(filt):
    """The payload keys the filter accepts as missing."""
    keys = []
    for condition in filt.must or []:
        for branch in getattr(condition, "should", None) or []:
            is_empty = getattr(branch, "is_empty", None)
            if is_empty is not None:
                keys.append(is_empty.key)
    return keys


class TestValidityConditions:
    @pytest.mark.unit
    def test_bounds_both_ends_around_the_given_day(self):
        filt = qmodels.Filter(must=_validity_conditions(TODAY))

        assert sorted(dates_in(filt)) == [
            ("end_date", "gte", TODAY),
            ("start_date", "lte", TODAY),
        ]

    @pytest.mark.unit
    def test_bounds_are_inclusive(self):
        # lte/gte, not lt/gt: a document starting today answers today, and its
        # end date names the last day it answers.
        filt = qmodels.Filter(must=_validity_conditions(TODAY))

        assert {op for _, op, _ in dates_in(filt)} == {"lte", "gte"}

    @pytest.mark.unit
    def test_accepts_a_chunk_missing_either_date(self):
        # This is what keeps the pre-validity corpus searchable.
        filt = qmodels.Filter(must=_validity_conditions(TODAY))

        assert sorted(empty_keys_in(filt)) == ["end_date", "start_date"]


class TestBuildFilter:
    @pytest.mark.unit
    def test_adds_validity_when_a_day_is_given(self):
        filt = _build_filter(valid_on=TODAY)

        assert len(dates_in(filt)) == 2

    @pytest.mark.unit
    def test_leaves_validity_out_when_no_day_is_given(self):
        # The delete/list paths: an expired chunk is still that document's
        # chunk, and a purge that skipped expired chunks would orphan them.
        assert _build_filter(doc_id="doc-1") is not None
        assert dates_in(_build_filter(doc_id="doc-1")) == []

    @pytest.mark.unit
    def test_returns_none_when_nothing_is_constrained(self):
        assert _build_filter() is None

    @pytest.mark.unit
    def test_validity_alone_is_enough_to_build_a_filter(self):
        assert _build_filter(valid_on=TODAY) is not None

    @pytest.mark.unit
    def test_composes_with_the_reference_exclusion(self):
        filt = _build_filter(exclude_reference=True, valid_on=TODAY)
        keys = [getattr(c, "key", None) for c in filt.must]

        assert "is_reference" in keys
        assert len(dates_in(filt)) == 2


class TestSearchAppliesValidity:
    @pytest.mark.unit
    def test_filters_on_the_injected_clocks_today(self):
        client = FakeClient()
        store = QdrantVectorStore(client=client, clock=clock_at(TODAY))

        store.search("idx", "kisan", search_mode="LEXICAL")

        assert sorted(dates_in(client.scroll_filter)) == [
            ("end_date", "gte", TODAY),
            ("start_date", "lte", TODAY),
        ]

    @pytest.mark.unit
    def test_reports_the_day_it_applied(self):
        store = QdrantVectorStore(client=FakeClient(), clock=clock_at(TODAY))

        result = store.search("idx", "kisan", search_mode="LEXICAL")

        assert result["valid_on"] == TODAY

    @pytest.mark.unit
    def test_valid_on_overrides_the_clock(self):
        client = FakeClient()
        store = QdrantVectorStore(client=client, clock=clock_at(TODAY))

        result = store.search("idx", "kisan", search_mode="LEXICAL", valid_on="2027-01-01")

        assert result["valid_on"] == "2027-01-01"
        assert ("start_date", "lte", "2027-01-01") in dates_in(client.scroll_filter)

    @pytest.mark.unit
    def test_apply_validity_false_drops_the_period_filter(self):
        client = FakeClient()
        store = QdrantVectorStore(client=client, clock=clock_at(TODAY))

        result = store.search(
            "idx", "kisan", search_mode="LEXICAL", exclude_reference=False, apply_validity=False
        )

        assert result["valid_on"] is None
        assert client.scroll_filter is None

    @pytest.mark.unit
    def test_validity_is_on_by_default(self):
        # A caller that forgets to ask must not serve an expired document.
        client = FakeClient()
        store = QdrantVectorStore(client=client, clock=clock_at(TODAY))

        store.search("idx", "kisan", search_mode="LEXICAL")

        assert len(dates_in(client.scroll_filter)) == 2

    @pytest.mark.unit
    def test_defaults_to_the_real_clock(self):
        store = QdrantVectorStore(client=FakeClient())

        assert store.today() == datetime.now().date().isoformat()


class TestPayload:
    @pytest.mark.unit
    def test_carries_the_period_onto_the_point(self):
        payload = _record_payload(
            {"text": "x", "start_date": "2026-09-22", "end_date": "2027-09-22"}
        )

        assert payload["start_date"] == "2026-09-22"
        assert payload["end_date"] == "2027-09-22"

    @pytest.mark.unit
    def test_omits_the_period_when_the_record_has_none(self):
        # Written as absent rather than null, which is what the missing-field
        # branch of the validity filter matches on.
        payload = _record_payload({"text": "x"})

        assert "start_date" not in payload
        assert "end_date" not in payload

    @pytest.mark.unit
    def test_the_period_is_a_declared_payload_field(self):
        assert {"start_date", "end_date"} <= set(PAYLOAD_FIELDS)

    @pytest.mark.unit
    def test_the_period_is_indexed_as_a_datetime(self):
        assert (
            QdrantVectorStore.PAYLOAD_INDEXES["start_date"]
            == qmodels.PayloadSchemaType.DATETIME
        )
        assert (
            QdrantVectorStore.PAYLOAD_INDEXES["end_date"]
            == qmodels.PayloadSchemaType.DATETIME
        )
