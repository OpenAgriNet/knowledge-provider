"""Scheme question understanding, ported from the BV provider's scheme-query.util.spec.ts."""

import pytest

from pipeline.search import scheme_query
from pipeline.search.scheme_query import (
    classify_query_intent,
    classify_section_focus,
    expand_query_for_search,
    query_names_unindexed_scheme,
    resolve_scheme_code,
)

REGISTRY = [
    {"scheme_code": "mif", "scheme_name": "Micro Irrigation Fund",
     "scheme_aliases": ["MIF", "micro irrigation fund", "Micro Irrigation Fund scheme"]},
    {"scheme_code": "e-nam", "scheme_name": "Electronic National Agriculture Market",
     "scheme_aliases": ["e-NAM", "eNAM", "enam", "national agriculture market", "electronic nam",
                        "electronic national agriculture market"]},
    {"scheme_code": "pk", "scheme_name": "PM Kisan", "scheme_aliases": []},
    {"scheme_code": "pmkmy", "scheme_name": "Pradhan Mantri Kisan Maandhan Yojana", "scheme_aliases": ["PM Kisan Maandhan"]},
]


# --- Cases carried over unchanged from the BV provider's spec ---------------

def test_resolves_mif_alias():
    assert resolve_scheme_code("Micro Irrigation Fund eligibility", REGISTRY) == "mif"
    assert resolve_scheme_code("what is MIF subsidy", REGISTRY) == "mif"


def test_resolves_e_nam_aliases():
    assert resolve_scheme_code("eNAM registration process", REGISTRY) == "e-nam"
    assert resolve_scheme_code("electronic national agriculture market", REGISTRY) == "e-nam"


def test_classifies_eligibility_intent_and_section_focus():
    assert classify_query_intent("who is eligible for MIF") == "eligibility"
    assert classify_section_focus("who is eligible for MIF") == "eligibility_with_exclusion"
    assert classify_section_focus("who cannot apply for MIF") == "exclusion_only"


def test_expands_eligibility_queries():
    query = "MIF eligibility"
    expanded = expand_query_for_search(query, classify_query_intent(query), classify_section_focus(query))
    assert "eligibility" in expanded.lower() and len(expanded) > len(query)


# --- Behaviour the port must keep ------------------------------------------

def test_longest_matching_label_wins():
    # "pm kisan" (8) and "pm kisan maandhan" (17) both appear; the longer one names the scheme.
    assert resolve_scheme_code("PM Kisan Maandhan pension amount", REGISTRY) == "pmkmy"
    assert resolve_scheme_code("PM Kisan overview", REGISTRY) == "pk"


def test_short_codes_must_stand_alone_as_words():
    assert resolve_scheme_code("what is pk", REGISTRY) == "pk"
    assert resolve_scheme_code("pkvy organic farming", REGISTRY) is None
    assert resolve_scheme_code("mifflin county", REGISTRY) is None


def test_generic_words_never_resolve_a_scheme():
    registry = [{"scheme_code": "nf", "scheme_name": "Mission", "scheme_aliases": ["scheme", "yojana"]}]
    assert resolve_scheme_code("which scheme or yojana or mission helps me", registry) is None


@pytest.mark.parametrize("query,expected", [
    ("Gobar Dhan eligibility", True),          # names a topic no indexed scheme matches
    ("who is eligible for this scheme", False),  # only intent and generic words
    ("PM Kisan eligibility", False),            # resolves, so it is not unindexed
])
def test_unindexed_scheme_detection(query, expected):
    assert query_names_unindexed_scheme(query, REGISTRY) is expected


@pytest.mark.parametrize("query,intent,focus,expansion", [
    ("how to apply for PM Kisan", "application", None, "application"),
    ("how much subsidy under MIF", "support", None, "support"),
    ("who is excluded from PM Kisan", None, "exclusion_only", "exclusion"),
    ("PM Kisan overview", None, None, None),
])
def test_intent_focus_and_expansion(query, intent, focus, expansion):
    assert classify_query_intent(query) == intent
    assert classify_section_focus(query) == focus
    expanded = expand_query_for_search(query, intent, focus)
    if expansion is None:
        assert expanded == query
    else:
        assert expanded == f"{query} {scheme_query.QUERY_EXPANSIONS[expansion]}"


def test_registry_merges_documents_and_respects_visibility(monkeypatch):
    rows = [
        {"code": "PK", "name": "PM Kisan", "aliases": ["Kisan Samman Nidhi"], "instances": ["bv"]},
        {"code": "pk", "name": "PM Kisan", "aliases": [], "instances": ["mh"]},
        {"code": "kcc", "name": "Kisan Credit Card", "aliases": [], "instances": ["ka"]},
        {"code": "", "name": "No code", "aliases": [], "instances": ["bv"]},
    ]
    monkeypatch.setattr("pipeline.db.list_catalog_entries", lambda **_: rows)
    everything = scheme_query.load_scheme_registry(network_id=None, group=None, permitted=None)
    assert [entry["scheme_code"] for entry in everything] == ["pk", "kcc"]
    assert "Kisan Samman Nidhi" in everything[0]["scheme_aliases"] and "PM Kisan" in everything[0]["scheme_aliases"]
    assert [e["scheme_code"] for e in scheme_query.load_scheme_registry(network_id=None, group="ka", permitted=None)] == ["kcc"]
    assert [e["scheme_code"] for e in scheme_query.load_scheme_registry(network_id=None, group=None, permitted={"mh"})] == ["pk"]


# --- Step 2: sections, re-ranking and final order ---------------------------

from pipeline.search.scheme_query import (  # noqa: E402
    classify_chunk_section,
    finalize_scheme_hits,
    rerank_scheme_hits,
    supplemental_section,
)


@pytest.mark.parametrize("text,section", [
    ("## Exclusion\nInstitutional land holders are excluded", "exclusion"),
    ("## Eligibility criteria\nAll landholding farmer families", "eligibility"),
    ("Scheme eligibility and who cannot apply", "eligibility"),     # tie: eligibility wins
    ("## How to apply\nRegister on the online portal", "application"),
    ("## Benefits\nRs. 6000 per year", "support"),
    ("Persons found ineligible must refund", "exclusion"),
    ("Farmers are eligible after verification", "eligibility"),     # keyword fallback
    ("Registration happens at the CSC", "application"),
    ("The subsidy is released in instalments", "support"),
    ("The scheme was launched in 2019", "other"),
])
def test_classifies_chunk_sections(text, section):
    assert classify_chunk_section(text) == section


@pytest.mark.parametrize("focus,intent,needed", [
    ("eligibility_with_exclusion", "eligibility", "exclusion"),
    (None, "support", "support"),
    (None, "application", "application"),
    ("exclusion_only", None, None),
    (None, None, None),
])
def test_supplemental_section(focus, intent, needed):
    assert supplemental_section(focus, intent) == needed


def hit(chunk_id, text, score=0.5, section=None):
    return {"chunk_id": chunk_id, "text": text, "_score": score, "section_type": section}


def order(hits):
    return [h["chunk_id"] for h in hits]


def test_eligibility_answers_prefer_farmer_text_over_cost_tables():
    table = hit("table", "| Item | Rs. 5000 per ha | cost norm | pattern of assistance |", score=0.62)
    farmer = hit("farmer", "## Eligibility\nAll small and marginal farmer families are eligible", score=0.55)
    ranked = rerank_scheme_hits("who is eligible for MIF", [table, farmer], "eligibility")
    assert order(ranked) == ["farmer", "table"]
    assert ranked[0]["_scheme_score"] > ranked[1]["_scheme_score"]


def test_application_answers_penalise_app_ui_text():
    ui = hit("ui", "Click on my profile, then logout from the user manual screen", score=0.6)
    steps = hit("steps", "How to apply:\n1. Visit the online portal\n2. Upload documents required", score=0.5)
    assert order(rerank_scheme_hits("how to apply for PM Kisan", [ui, steps], "application")) == ["steps", "ui"]


def test_subsidy_questions_welcome_tables_other_support_questions_do_not():
    table = hit("table", "| Component | Rs. 2000 per ha | pattern of assistance |", score=0.5)
    prose = hit("prose", "The scheme gives support to farmers", score=0.5)
    assert order(rerank_scheme_hits("how much subsidy under MIF", [prose, table], "support")) == ["table", "prose"]
    assert order(rerank_scheme_hits("what support does MIF give", [table, prose], "support")) == ["prose", "table"]


def test_institutional_text_drops_without_an_intent():
    admin = hit("admin", "The state level executive committee and implementing agency review plans", score=0.6)
    plain = hit("plain", "PM Kisan pays farmers directly", score=0.55)
    assert order(rerank_scheme_hits("PM Kisan overview", [admin, plain], None)) == ["plain", "admin"]


def test_finalize_balances_eligibility_and_exclusion():
    # The BV provider's own case: two slots, one per section.
    hits = [hit("1", "a", section="eligibility"), hit("2", "b", section="eligibility"),
            hit("3", "c", section="exclusion"), hit("4", "d", section="other")]
    assert {h["section_type"] for h in finalize_scheme_hits(hits, "eligibility_with_exclusion", None, 2)} == {
        "eligibility", "exclusion"}
    assert order(finalize_scheme_hits(hits, "eligibility_with_exclusion", None, 4)) == ["1", "2", "3", "4"]


def test_finalize_puts_the_asked_section_first_and_drops_duplicates():
    hits = [hit("1", "a", section="other"), hit("2", "b", section="exclusion"),
            hit("1", "a", section="other"), hit("3", "c", section="support")]
    assert order(finalize_scheme_hits(hits, "exclusion_only", None, 10)) == ["2", "1", "3"]
    assert order(finalize_scheme_hits(hits, None, "support", 2)) == ["3", "1"]
    assert order(finalize_scheme_hits(hits, None, None, 10)) == ["1", "2", "3"]
