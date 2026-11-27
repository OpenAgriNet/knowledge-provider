"""Advisory search rules, ported from the BV provider's advisory-qdrant.util.spec.ts."""

import copy
from pathlib import Path

import pytest

from pipeline.metadata.knowledge_metadata import KnowledgeMetadata
from pipeline.search import advisory_query as aq
from pipeline.search.passage_projection import _prepare_records
from tests.icar_webhook_fixture import SORGHUM_PAYLOAD


def hit(chunk, score, doc="d1", pop=None, season_keys=None, **advisory):
    """A search hit; entry, window and place live under the payload's advisory key."""
    if pop is not None:
        advisory["pop_id"] = pop
    return {"chunk_id": chunk, "doc_id": doc, "_score": score,
            **({"season_keys": season_keys} if season_keys is not None else {}),
            **({"advisory": advisory} if advisory else {})}


def ids(hits):
    return [h["chunk_id"] for h in hits]


# --- dedupe ------------------------------------------------------------------

def test_dedupe_keeps_best_passage_per_entry_sorted_by_score():
    hits = [hit("a", 0.5, pop=1), hit("b", 0.9, pop=1), hit("c", 0.7, pop=2), hit("d", 0.8, doc="d2", pop=1)]
    assert ids(aq.dedupe_by_entry(hits)) == ["b", "d", "c"]


def test_dedupe_keeps_every_passage_without_an_entry_id():
    assert ids(aq.dedupe_by_entry([hit("x", 0.4), hit("y", 0.6)])) == ["y", "x"]
    assert aq.dedupe_by_entry([]) == []


# --- request -----------------------------------------------------------------

@pytest.mark.parametrize("raw,crop", [
    ("wheat", "Wheat"), ("GEHUN", "Wheat"), ("गेहूं", "Wheat"), ("dhan", "Rice"), ("jowar", "Sorghum"),
    ("kodo-millet", "Kodo Millet"), ("black  gram", "Black Gram"), ("", ""), (None, ""),
])
def test_normalize_crop(raw, crop):
    assert aq.normalize_crop(raw) == crop


def test_preferred_season_env_wins_over_request(monkeypatch):
    monkeypatch.delenv("ADVISORY_DEFAULT_SEASON", raising=False)
    assert aq.preferred_season("Kharif") == "Kharif" and aq.preferred_season(None) == ""
    monkeypatch.setenv("ADVISORY_DEFAULT_SEASON", "Rabi")
    assert aq.preferred_season("Kharif") == "Rabi"


# --- ranking -----------------------------------------------------------------

def rank(hits, season="", days=None, state=None, district=None):
    return aq.rank_advisory_hits(hits, season=season, days_after_sowing=days, state=state, district=district)


def test_season_preference_reorders_without_dropping():
    hits = [hit("kharif", 0.80, season_keys=["kharif"]), hit("rabi", 0.75, season_keys=["rabi"])]
    assert ids(rank(hits, season="Rabi")) == ["rabi", "kharif"]
    assert ids(rank(hits)) == ["kharif", "rabi"]
    assert ids(rank([hit("multi", 0.7, season_keys=["rabi,kharif"]), hit("x", 0.75)], season="kharif"))[0] == "multi"


def test_sowing_window_is_inclusive_and_ignores_missing_ranges():
    inside = hit("inside", 0.70, start_period=105, end_period=120)
    edge = hit("edge", 0.70, start_period=90, end_period=105)
    none = hit("none", 0.80)
    assert ids(rank([none, inside, edge], days=105)) == ["inside", "edge", "none"]
    assert aq.has_day_range(inside) and not aq.has_day_range(none)


def located(chunk, score, states=(), statewide=(), districts=(), national=False):
    return hit(chunk, score, location_national=national, location_state_codes=list(states),
               location_statewide_codes=list(statewide), location_district_codes=list(districts))


@pytest.mark.parametrize("advisory,state,district,match", [
    (hit("legacy", 0.5), 27, None, "national"),                               # indexed before location existed
    (located("n", 0.5, states=[27], national=True), 27, None, "national"),
    (located("d", 0.5, states=[27], districts=[519]), None, 519, "matched"),  # district alone matches
    (located("s", 0.5, states=[27], statewide=[27]), 27, 600, "matched"),     # statewide covers any district
    (located("p", 0.5, states=[27], districts=[519]), 27, None, "state_partial"),
    (located("o", 0.5, states=[9], districts=[100]), 27, 519, "other"),
    (located("u", 0.5, states=[27], statewide=[27]), None, None, "not_requested"),
])
def test_classify_location(advisory, state, district, match):
    assert aq.classify_location(advisory, state, district) == match


def test_place_boosts_matched_more_than_partial_and_keeps_everything():
    hits = [located("other", 0.80, states=[9], districts=[100]), located("partial", 0.72, states=[27], districts=[519]),
            located("statewide", 0.70, states=[27], statewide=[27])]
    ranked = rank(hits, state=27)
    assert ids(ranked) == ["statewide", "partial", "other"]
    assert [h["location_match"] for h in ranked] == ["matched", "state_partial", "other"]
    assert aq.location_status(ranked, True) == "location_matched"
    assert aq.location_status(ranked, False) == "not_requested" and aq.location_status([], True) == "no_match"


# --- indexing ----------------------------------------------------------------

AREAS = [
    {"codeScheme": "LGD", "areaCode": "27", "areaLevel": "state", "areaName": "Maharashtra"},
    {"codeScheme": "LGD", "areaCode": "519", "areaLevel": "district", "areaName": "Pune"},
]
TEXT = "Problem: Method Of Harvesting\n\nStart day relative to sowing: 105\n\nEnd day relative to sowing: 120"


def advisory_records(areas=AREAS, text=TEXT, source_type="webhook", kind="advisory"):
    metadata = {"subject_categories": ["Crop"], "seasons": ["Rabi"], "coverage_areas": areas,
                "attributes": {"source_record": {"content_id": 987, "pop_id": 819}}}
    return _prepare_records("doc-1", "987:819", [{"chunk_number": 1, "original_text": text}],
                            kind=kind, knowledge_metadata=metadata, source_type=source_type)


def test_advisory_passages_carry_entry_window_place_and_origin():
    record = advisory_records()[0]
    fields = record["advisory"]
    assert (fields["pop_id"], fields["start_period"], fields["end_period"]) == (819, 105, 120)
    assert fields["location_national"] is False
    assert (fields["location_state_codes"], fields["location_district_codes"]) == ([27], [519])
    assert fields["location_statewide_codes"] == []          # districts are listed, so not statewide
    assert (fields["location_state_names"], fields["location_district_names"]) == (["Maharashtra"], ["Pune"])
    assert record["source"] == "webhook" and "pop_id" not in record   # no legacy top-level fields


def test_state_without_districts_is_statewide_and_no_place_is_national():
    statewide = advisory_records(areas=AREAS[:1])[0]["advisory"]
    assert statewide["location_statewide_codes"] == [27]
    national = advisory_records(areas=[], text="no window")[0]["advisory"]
    assert national["location_national"] is True and "start_period" not in national


def test_uploads_keep_docs_pipeline_origin_and_other_kinds_get_no_advisory_fields():
    assert advisory_records(source_type="document")[0]["source"] == "docs-pipeline"
    scheme = advisory_records(kind="scheme")[0]
    assert scheme["source"] == "webhook" and "advisory" not in scheme


_PARENTS = Path(__file__).resolve().parents
KIT_TEMPLATE = (_PARENTS[3] if len(_PARENTS) > 3 else Path("/")) / "bh-prod-data/migrations/templates/icar-advisory.jsonata"


@pytest.mark.skipif(not KIT_TEMPLATE.is_file(), reason="restore kit template not beside the checkout")
def test_real_icar_intake_template_feeds_the_advisory_fields():
    import jsonata

    payload = copy.deepcopy(SORGHUM_PAYLOAD)
    payload["contentLocationDetails"] = [{"stateLgdCode": 27, "stateName": "Maharashtra",
                                          "districts": [{"districtLgdCode": 519, "districtName": "Pune"}]}]
    [item] = jsonata.Jsonata(KIT_TEMPLATE.read_text()).evaluate(payload)
    KnowledgeMetadata.model_validate(item["knowledge_metadata"])
    [record] = _prepare_records("doc-1", item["external_id"], [{"chunk_number": 1, "original_text": item["text"]}],
                                kind=item["kind"], knowledge_metadata=item["knowledge_metadata"], source_type="webhook")
    fields = record["advisory"]
    assert fields["pop_id"] == 819 and "start_period" not in fields and "end_period" not in fields
    assert record["metadata"]["attributes"]["applicability"] == [
        {"relativeTiming": {"event": "Sowing", "fromDays": 105, "toDays": 120}},
    ]
    assert aq.days_in_range(record, 105) and aq.days_in_range(record, 120)
    assert not aq.days_in_range(record, 121)
    assert (fields["location_district_codes"], record["season_keys"]) == ([519], ["rabi"])
    assert "sorghum" in record["crop_keys"]


@pytest.mark.parametrize(("rules", "days", "matched"), [
    ([{"relativeTiming": {"event": "Sowing", "fromDays": 105}}], 120, True),
    ([{"relativeTiming": {"event": "Sowing", "toDays": 120}}], -1, False),
    ([{"relativeTiming": {"event": "Sowing", "toDays": 120}}], 120, True),
    ([{"relativeTiming": {"event": "Transplanting", "fromDays": 105}}], 120, False),
    ([{"relativeTiming": {"event": "Sowing", "fromDays": 105}, "waterRegimes": ["Irrigated"]}], 120, False),
    ([{"relativeTiming": {"event": "Sowing", "fromDays": 120, "toDays": 105}}], 110, False),
])
def test_structured_sowing_rules_take_precedence_over_legacy_text(rules, days, matched):
    record = hit("example", 0.70, start_period=-10, end_period=180)
    record["metadata"] = {"attributes": {"applicability": rules}}
    assert aq.days_in_range(record, days) is matched
