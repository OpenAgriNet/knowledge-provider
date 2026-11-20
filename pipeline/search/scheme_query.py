"""Scheme question understanding for the ``scheme`` search profile.

Ported from the BV provider (bharat-provider-backend ``scheme-query.util.ts``)
so this API answers scheme questions the way the BV consumer did: resolve the
scheme a question names, recognise what is being asked (eligibility,
application, support, exclusion), and widen the embedding query toward the
guideline sections that answer it. Matching is English keyword based, as in
the source.
"""

from __future__ import annotations

import re
from typing import TypedDict


class SchemeEntry(TypedDict):
    scheme_code: str
    scheme_name: str
    scheme_aliases: list[str]


ALIAS_STOPWORDS = frozenset({
    "scheme", "schemes", "mission", "national", "programme", "program", "yojana", "yojna",
    "development", "fund", "for", "the", "and", "under", "government", "india", "ministry",
    "guidelines", "operational",
})

SCHEME_FOCUS_STOPWORDS = ALIAS_STOPWORDS | {
    "what", "who", "how", "when", "where", "why", "which", "about", "farmer", "farmers",
    "criteria", "details", "information", "available", "tell", "explain", "know", "need",
    "want", "there", "this", "that",
}

HOW_TO_APPLY = "how to apply"
APPLICATION_PROCESS = "application process"
DOCUMENTS_REQUIRED = "documents required"
COST_NORM = "cost norm"
PATTERN_OF_ASSISTANCE = "pattern of assistance"

# Order matters: on a tie the earlier intent wins, as in the source.
INTENT_TERMS: dict[str, tuple[str, ...]] = {
    "eligibility": (
        "eligibility", "eligible", "am i eligible", "who can apply", "who is eligible", "qualify",
        "criteria", "qualifying", "for selection of",
    ),
    "application": (
        HOW_TO_APPLY, APPLICATION_PROCESS, "application procedure", "application", "apply",
        "register", "registration", "portal", DOCUMENTS_REQUIRED, "how do i apply", "apply for",
        "procedure", "process",
    ),
    "support": (
        "support", "financial support", "government support", "what support", "assistance",
        "financial assistance", "subsidy", "subsidies", "benefit", "benefits", "how much", "fund",
        "incentive", PATTERN_OF_ASSISTANCE, COST_NORM, "help available",
    ),
}

EXCLUSION_TERMS: tuple[str, ...] = (
    "excluded", "exclusion", "who cannot apply", "who is excluded", "who are excluded",
    "not eligible", "ineligible", "cannot apply",
)

QUERY_EXPANSIONS: dict[str, str] = {
    "eligibility": (
        "farmer beneficiary selection criteria who is eligible groups clusters scheme exclusion "
        "who cannot apply ineligible not eligible"
    ),
    "exclusion": "scheme exclusion who cannot apply ineligible not eligible excluded",
    "application": (
        f"{APPLICATION_PROCESS} registration portal {DOCUMENTS_REQUIRED} {HOW_TO_APPLY} "
        "apply online procedure steps"
    ),
    "support": (
        f"scheme benefits subsidy assistance financial support {PATTERN_OF_ASSISTANCE} {COST_NORM} "
        "incentive fund amount"
    ),
}

EXCLUSION_ONLY = "exclusion_only"
ELIGIBILITY_WITH_EXCLUSION = "eligibility_with_exclusion"
REGISTRATION_PROCESS = "registration process"

FARMER_TERMS = (
    "farmer", "farmers", "farmer's", "farmers'", "farmers' groups", "farmer groups", "individual farmer",
    "all farmers", "small and marginal", "land holder", "cultivator", "for selection of areas",
    "for selection of areas/ farmers",
)
INSTITUTIONAL_TERMS = (
    "regional council", "support agency", "implementing agency", "state government", "state/ut",
    "organisation/agency", "organization/agency", "steering committee", "executive committee",
    "district level executive", "state technical unit", "project management team", "state level executive",
    "technical assistant of agencies", "seed growers covered",
)
APP_UI_NOISE_TERMS = (
    "click on", "logout", "offline record", "sync (offline", "registered farmer's list", "user manual",
    "app video guide", "language change", "my profile", "triple bar icon",
)
APPLICATION_CHUNK_TERMS = (HOW_TO_APPLY, APPLICATION_PROCESS, DOCUMENTS_REQUIRED, "registration")
APPLICATION_RERANK_PHRASES = (
    HOW_TO_APPLY, APPLICATION_PROCESS, REGISTRATION_PROCESS, DOCUMENTS_REQUIRED, "online portal", "apply through",
)
SUPPORT_CHUNK_TERMS = ("support", "subsidy", "assistance", "benefit", COST_NORM)
SUBSIDY_QUERY_TERMS = ("subsidy", "assistance", COST_NORM, "how much")
SUPPORT_RERANK_TERMS = ("benefit", "subsidy", "assistance", "support", "incentive")
HEADING_FARMER_ELIGIBILITY_MARKERS = (
    "# eligibility", "# eligible", "# criteria", "## eligibility", "## eligible", "## criteria",
)
RE_HEADING_ELIGIBILITY = re.compile(r"#{1,6}\s+(?:eligibility|eligible criteria|who can apply)", re.IGNORECASE)
RE_HEADING_EXCLUSION = re.compile(r"#{1,6}\s+(?:exclusion|scheme exclusion|who cannot apply)", re.IGNORECASE)
RE_HEADING_SUPPORT = re.compile(r"#{1,6}\s+(?:benefits|support|pattern of assistance)", re.IGNORECASE)
RE_NUMBERED_STEPS = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
RE_RUPEES = re.compile(r"rs\.?\s*\d", re.IGNORECASE)

# Section patterns in priority order: on a tie the earlier section wins.
SECTION_PATTERNS: dict[str, tuple[re.Pattern, ...]] = {
    section: tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)
    for section, patterns in {
        "exclusion": (
            r"#+\s*scheme exclusion", r"#+\s*exclusion", "scheme exclusion", "who are excluded", "who cannot apply",
            "not eligible for", "ineligible",
        ),
        "eligibility": (
            r"#+\s*scheme eligibility", r"#+\s*eligibility", "scheme eligibility", "who can apply", "who is eligible",
            "eligible criteria", "eligibility criteria",
        ),
        "application": (
            rf"#+\s*{re.escape(APPLICATION_PROCESS)}", rf"#+\s*{re.escape(HOW_TO_APPLY)}", re.escape(APPLICATION_PROCESS),
            re.escape(HOW_TO_APPLY), re.escape(DOCUMENTS_REQUIRED), re.escape(REGISTRATION_PROCESS), "online portal",
        ),
        "support": (
            r"#+\s*benefits", r"#+\s*support", "scheme benefits", re.escape(PATTERN_OF_ASSISTANCE), "financial support",
            re.escape(COST_NORM), "subsidy", "assistance to farmers",
        ),
    }.items()
}
# The guideline section a question type is answered from.
INTENT_SECTIONS = {"support": "support", "application": "application"}


def _alias_in_query(alias: str, query: str) -> bool:
    # Short aliases (PK, MIF) and hyphenated ones (e-NAM) must stand alone as
    # words; longer names may appear inside the question as a phrase.
    # re.ASCII keeps word boundaries identical to the JavaScript original.
    if len(alias) <= 6 or "-" in alias:
        return re.search(rf"\b{re.escape(alias)}\b", query, re.IGNORECASE | re.ASCII) is not None
    return alias in query


def resolve_scheme_code(query: str, registry: list[SchemeEntry]) -> str | None:
    """Code of the scheme whose longest code, name or alias appears in the question."""
    lowered = query.lower()
    best_code, best_length = None, 0
    for entry in registry:
        for candidate in (entry["scheme_code"], entry["scheme_name"], *entry["scheme_aliases"]):
            alias = str(candidate or "").lower().strip()
            if not alias or alias in ALIAS_STOPWORDS or not _alias_in_query(alias, lowered):
                continue
            if len(alias) > best_length:
                best_code, best_length = entry["scheme_code"], len(alias)
    return best_code


def _strip_intent_terms(query: str) -> str:
    cleaned = query.lower()
    for terms in INTENT_TERMS.values():
        for term in sorted(terms, key=len, reverse=True):
            cleaned = cleaned.replace(term, " ")
    for term in EXCLUSION_TERMS:
        cleaned = cleaned.replace(term, " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def query_names_unindexed_scheme(query: str, registry: list[SchemeEntry]) -> bool:
    """True when the question still names a topic after intent words are removed
    but no indexed scheme matches it - so a scheme outside the index is not
    answered with a different scheme's guideline."""
    if resolve_scheme_code(query, registry):
        return False
    tokens = re.split(r"[\s?,]+", _strip_intent_terms(query))
    return any(len(token) >= 3 and token not in SCHEME_FOCUS_STOPWORDS for token in tokens)


def classify_query_intent(query: str) -> str | None:
    lowered = query.lower()
    best_intent, best_score = None, 0
    for intent, terms in INTENT_TERMS.items():
        score = sum(term in lowered for term in terms)
        if score > best_score:
            best_intent, best_score = intent, score
    return best_intent


def classify_section_focus(query: str) -> str | None:
    lowered = query.lower()
    has_eligibility = any(term in lowered for term in INTENT_TERMS["eligibility"])
    has_exclusion = any(term in lowered for term in EXCLUSION_TERMS)
    if has_exclusion and not has_eligibility:
        return EXCLUSION_ONLY
    return ELIGIBILITY_WITH_EXCLUSION if has_eligibility else None


def expand_query_for_search(query: str, intent: str | None, section_focus: str | None) -> str:
    """Append the guideline vocabulary of the asked section before embedding."""
    if section_focus == EXCLUSION_ONLY:
        return f"{query} {QUERY_EXPANSIONS['exclusion']}"
    if intent in QUERY_EXPANSIONS:
        return f"{query} {QUERY_EXPANSIONS[intent]}"
    return query


def classify_chunk_section(text: str) -> str:
    """Which guideline section a passage reads like: eligibility, exclusion,
    application, support or other. Read from the text at query time, so it
    needs no re-indexing."""
    lowered = (text or "").lower()
    scores = {section: sum(bool(p.search(lowered)) for p in patterns) for section, patterns in SECTION_PATTERNS.items()}
    best = max(scores, key=scores.get)
    if scores[best] > 0:
        # Exclusion wins only outright; a tie with eligibility reads as eligibility.
        if best == "exclusion" and scores["exclusion"] <= scores["eligibility"]:
            return "eligibility" if scores["eligibility"] > 0 else "exclusion"
        return best
    if any(term in lowered for term in ("exclusion", "excluded", "cannot apply", "ineligible")):
        return "exclusion"
    if any(term in lowered for term in ("eligibility", "eligible", "who can apply")):
        return "eligibility"
    if any(term in lowered for term in APPLICATION_CHUNK_TERMS):
        return "application"
    if any(term in lowered for term in SUPPORT_CHUNK_TERMS):
        return "support"
    return "other"


def with_section_types(hits: list[dict]) -> list[dict]:
    return [{**hit, "section_type": classify_chunk_section(str(hit.get("text") or ""))} for hit in hits]


def supplemental_section(section_focus: str | None, intent: str | None) -> str | None:
    """The section a second search should fetch when the first found none of it."""
    if section_focus == ELIGIBILITY_WITH_EXCLUSION:
        return "exclusion"
    return INTENT_SECTIONS.get(intent or "")


def _table_score(text: str) -> float:
    lowered = text.lower()
    pipe_density = text.count("|") / max(len(text), 1)
    score = min(pipe_density * 10, 0.55)
    if RE_RUPEES.search(lowered):
        score += 0.12
    if "/ha" in lowered or "per ha" in lowered or "per beneficiary" in lowered:
        score += 0.12
    if COST_NORM in lowered or PATTERN_OF_ASSISTANCE in lowered:
        score += 0.18
    if "annexure" in lowered and pipe_density > 0.015:
        score += 0.08
    return min(score, 1.0)


def _institutional_score(text: str) -> float:
    lowered = text.lower()
    return min(sum(term in lowered for term in INSTITUTIONAL_TERMS) * 0.09, 0.45)


def _farmer_score(text: str) -> float:
    lowered = text.lower()
    hits = sum(term in lowered for term in FARMER_TERMS)
    if "farmer" in lowered and any(marker in lowered for marker in HEADING_FARMER_ELIGIBILITY_MARKERS):
        hits += 2
    if "for selection of" in lowered and "farmer" in lowered:
        hits += 3
    if "3.3" in lowered and "criteria" in lowered and "#" in text:
        hits += 2
    return min(hits * 0.07, 0.4)


def _intent_bonus_penalty(
    intent: str | None, text: str, lowered: str, institutional: float, table: float, wants_tables: bool,
) -> tuple[float, float]:
    if intent == "eligibility":
        bonus = (0.1 if RE_HEADING_ELIGIBILITY.search(lowered) else 0) + (0.12 if RE_HEADING_EXCLUSION.search(lowered) else 0)
        if "regional council" in lowered and "eligible criteria" in lowered:
            institutional += 0.35
        return bonus, institutional + table * 0.85
    if intent == "application":
        bonus = (0.18 if any(p in lowered for p in APPLICATION_RERANK_PHRASES) else 0) + (
            0.05 if RE_NUMBERED_STEPS.search(text) else 0)
        ui_noise = sum(term in lowered for term in APP_UI_NOISE_TERMS)
        return bonus, institutional * 0.6 + table * 0.45 + min(ui_noise * 0.12, 0.45)
    if intent == "support":
        bonus = (0.12 if any(t in lowered for t in SUPPORT_RERANK_TERMS) else 0) + (
            0.1 if RE_HEADING_SUPPORT.search(lowered) else 0)
        penalty = institutional * 0.25
        if wants_tables:
            bonus += table * 0.15
        else:
            penalty += table * 0.2
        return bonus, penalty
    return 0.0, institutional * 0.25 + table * 0.25


def rerank_scheme_hits(query: str, hits: list[dict], intent: str | None) -> list[dict]:
    """Re-order passages for farmers reading scheme guidelines.

    Starts from the retrieval score, rewards the asked intent's words and
    farmer-facing text, and penalises administrative, tabular and app-UI text
    according to what was asked. The result is ``_scheme_score``."""
    lowered_query = query.lower()
    intent_terms = [term for terms in INTENT_TERMS.values() for term in terms if term in lowered_query]
    wants_tables = any(term in lowered_query for term in SUBSIDY_QUERY_TERMS)
    scored = []
    for hit in hits:
        text = str(hit.get("text") or "")
        lowered = text.lower()
        institutional, table = _institutional_score(text), _table_score(text)
        bonus, penalty = _intent_bonus_penalty(intent, text, lowered, institutional, table, wants_tables)
        keyword_hits = sum(term in lowered for term in intent_terms)
        score = float(hit.get("_score") or 0) + 0.1 * keyword_hits + _farmer_score(text) + bonus - penalty
        scored.append({**hit, "_scheme_score": score})
    return sorted(scored, key=lambda hit: hit["_scheme_score"], reverse=True)


def _hit_key(hit: dict, index: int) -> str:
    return str(hit.get("chunk_id") or hit.get("_id") or "") or f"idx-{index}"


def _prefer_section(hits: list[dict], section: str) -> list[dict]:
    return [h for h in hits if h.get("section_type") == section] + [h for h in hits if h.get("section_type") != section]


def _take_balanced(hits: list[dict], sections: tuple[str, ...], top_k: int) -> list[dict]:
    slots = max(top_k // len(sections), 1)
    chosen, used = [], set()
    for section in sections:
        taken = 0
        for index, hit in enumerate(hits):
            if taken >= slots:
                break
            if hit.get("section_type") == section and index not in used:
                used.add(index)
                chosen.append(hit)
                taken += 1
    for index, hit in enumerate(hits):
        if len(chosen) >= top_k:
            break
        if index not in used:
            used.add(index)
            chosen.append(hit)
    return chosen[:top_k]


def finalize_scheme_hits(hits: list[dict], section_focus: str | None, intent: str | None, top_k: int) -> list[dict]:
    """Final answer order: exclusions first for "who cannot apply", eligibility
    and exclusion side by side for eligibility questions, otherwise the asked
    section first."""
    seen, unique = set(), []
    for index, hit in enumerate(hits):
        key = _hit_key(hit, index)
        if key not in seen:
            seen.add(key)
            unique.append(hit)
    if section_focus == EXCLUSION_ONLY:
        return _prefer_section(unique, "exclusion")[:top_k]
    if section_focus == ELIGIBILITY_WITH_EXCLUSION:
        return _take_balanced(unique, ("eligibility", "exclusion"), top_k)
    if section := INTENT_SECTIONS.get(intent or ""):
        return _prefer_section(unique, section)[:top_k]
    return unique[:top_k]


def load_scheme_registry(
    *, network_id: str | None, group: str | None, permitted: set[str] | None,
) -> list[SchemeEntry]:
    """Live schemes the caller may see, one entry per code, with index-time aliases."""
    from pipeline import db
    from pipeline.network.catalog.scheme_catalog import resolve_scheme_aliases

    rows = db.list_catalog_entries(network_id=network_id) if network_id else db.list_catalog_entries(status="live")
    merged: dict[str, SchemeEntry] = {}
    for row in rows:
        code = (row.get("code") or "").strip().lower()
        instances = set(row.get("instances") or [])
        if not code or (group and group not in instances) or (permitted is not None and not permitted & instances):
            continue
        entry = merged.setdefault(code, {"scheme_code": code, "scheme_name": row.get("name") or code, "scheme_aliases": []})
        aliases = resolve_scheme_aliases(code, row.get("name"), row.get("aliases"))
        entry["scheme_aliases"] = list(dict.fromkeys([*entry["scheme_aliases"], *aliases]))
    return list(merged.values())
