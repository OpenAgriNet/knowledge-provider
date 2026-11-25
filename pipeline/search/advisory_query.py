"""Advisory search for ``document_kind="advisory"``.

Ported from the BV provider (bharat-provider-backend ``advisory-qdrant.util.ts``):
keep the best passage per advisory entry, then prefer advisories for the
farmer's season, crop stage (days after sowing) and place. Season, days and
place only reorder results; the crop is the one hard filter.
"""

from __future__ import annotations

import os
import re

from pipeline.metadata.knowledge_metadata import KnowledgeMetadata

INTAKE_ORIGINS = frozenset({"webhook", "crawl"})

SEASON_BOOST = 0.1
DAYS_BOOST = 0.15
LOCATION_BOOST_MATCHED = 0.2
LOCATION_BOOST_PARTIAL = 0.1

CROP_ALIASES = {
    "wheat": "Wheat", "gehun": "Wheat", "gehu": "Wheat", "गेहूं": "Wheat", "गेहू": "Wheat",
    "rice": "Rice", "paddy": "Rice", "dhan": "Rice", "धान": "Rice",
    "sorghum": "Sorghum", "jowar": "Sorghum", "ज्वार": "Sorghum",
    "kodo millet": "Kodo Millet", "kodo": "Kodo Millet", "kodo-millet": "Kodo Millet",
}

# The ICAR advisory intake template writes the sowing window into the passage
# text with these labels; reading them back needs no template change.
_START_DAY = re.compile(r"Start day relative to sowing:\s*(-?\d+)")
_END_DAY = re.compile(r"End day relative to sowing:\s*(-?\d+)")


# --- Indexing: flat payload fields the ranking (and the BV consumer) read ---

def advisory_payload(metadata: KnowledgeMetadata, chunks: list[dict]) -> dict:
    """Entry id, sowing window and LGD place of one advisory document, under
    the payload's ``advisory`` key."""
    fields: dict = {}
    source_record = metadata.attributes.get("source_record")
    if isinstance(source_record, dict) and source_record.get("pop_id") not in (None, ""):
        fields["pop_id"] = source_record["pop_id"]

    text = "\n".join(str(chunk.get("edited_text") or chunk.get("original_text") or "") for chunk in chunks)
    start, end = _START_DAY.search(text), _END_DAY.search(text)
    if start and end:
        fields["start_period"], fields["end_period"] = int(start.group(1)), int(end.group(1))

    states, districts = [], []
    for area in metadata.coverage_areas:
        if area.codeScheme.casefold() != "lgd" or not area.areaCode.lstrip("-").isdigit():
            continue
        level = (area.areaLevel or "").casefold()
        if level in ("state", "district"):
            (states if level == "state" else districts).append((int(area.areaCode), area.areaName or ""))
    # ponytail: the template does not record which state a district belongs
    # to, so a state counts as statewide only when no district is listed.
    # Exact for single-state advisories; record the parent state to lift it.
    fields.update({
        "location_national": not states and not districts,
        "location_state_codes": [code for code, _ in states],
        "location_statewide_codes": [] if districts else [code for code, _ in states],
        "location_district_codes": [code for code, _ in districts],
        "location_state_names": [name for _, name in states if name],
        "location_district_names": [name for _, name in districts if name],
    })
    return {"advisory": fields}


# --- Request ------------------------------------------------------------------

def normalize_crop(raw: str | None) -> str:
    value = " ".join(str(raw or "").split())
    if not value:
        return ""
    return CROP_ALIASES.get(value.lower()) or " ".join(word[:1].upper() + word[1:].lower() for word in value.split(" "))


def preferred_season(requested: str | None) -> str:
    """ADVISORY_DEFAULT_SEASON when set, else the caller's season; never a filter."""
    return (os.environ.get("ADVISORY_DEFAULT_SEASON") or "").strip() or (requested or "").strip()


# --- Ranking ------------------------------------------------------------------

def _int_list(value) -> list[int]:
    out = []
    for item in value if isinstance(value, list) else []:
        text = str(item).strip()
        if re.fullmatch(r"-?\d+", text):
            out.append(int(text))
    return out


def _period(value) -> int | None:
    text = str(value if value is not None else "").strip()
    return int(text) if re.fullmatch(r"-?\d+", text) else None


def _advisory(hit: dict) -> dict:
    fields = hit.get("advisory")
    return fields if isinstance(fields, dict) else {}


def season_list(hit: dict) -> list[str]:
    """An entry's seasons; several are stored as one value such as "Rabi,Kharif"."""
    return [part.strip().lower() for key in hit.get("season_keys") or [] for part in re.split(r"[,;/]", str(key)) if part.strip()]


def has_day_range(hit: dict) -> bool:
    return _period(_advisory(hit).get("start_period")) is not None and _period(_advisory(hit).get("end_period")) is not None


def days_in_range(hit: dict, days: int | None) -> bool:
    start, end = _period(_advisory(hit).get("start_period")), _period(_advisory(hit).get("end_period"))
    return days is not None and start is not None and end is not None and start <= days <= end


def is_national(hit: dict) -> bool:
    """Passages indexed before location existed carry no location fields: national."""
    fields = _advisory(hit)
    if fields.get("location_national", True) is True:
        return True
    return not (_int_list(fields.get("location_state_codes")) or _int_list(fields.get("location_statewide_codes"))
                or _int_list(fields.get("location_district_codes")))


def classify_location(hit: dict, state: int | None, district: int | None) -> str:
    """national, not_requested, matched, state_partial or other. District LGD
    codes are unique nationally, so a district matches without its state."""
    if is_national(hit):
        return "national"
    fields = _advisory(hit)
    if state is None and district is None:
        return "not_requested"
    if district is not None and district in _int_list(fields.get("location_district_codes")):
        return "matched"
    if state is not None and state in _int_list(fields.get("location_statewide_codes")):
        return "matched"
    if district is None and state is not None and state in _int_list(fields.get("location_state_codes")):
        return "state_partial"
    return "other"


def dedupe_by_entry(hits: list[dict]) -> list[dict]:
    """Best passage per advisory entry (document + pop id); passages without an
    entry id are kept one per chunk. Sorted by retrieval score."""
    best: dict[str, dict] = {}
    for hit in hits:
        pop = _advisory(hit).get("pop_id")
        key = f"pop:{hit.get('doc_id')}:{pop}" if pop not in (None, "") else f"chunk:{hit.get('chunk_id') or hit.get('_id')}"
        if key not in best or float(hit.get("_score") or 0) > float(best[key].get("_score") or 0):
            best[key] = hit
    return sorted(best.values(), key=lambda hit: float(hit.get("_score") or 0), reverse=True)


def rank_advisory_hits(
    hits: list[dict], *, season: str, days_after_sowing: int | None, state: int | None, district: int | None,
) -> list[dict]:
    """Soft boosts for season, sowing window and place; nothing is dropped."""
    wanted_season = season.strip().lower()
    ranked = []
    for hit in hits:
        score = float(hit.get("_score") or 0)
        if wanted_season and wanted_season in season_list(hit):
            score += SEASON_BOOST
        if days_in_range(hit, days_after_sowing):
            score += DAYS_BOOST
        match = classify_location(hit, state, district)
        score += LOCATION_BOOST_MATCHED if match == "matched" else LOCATION_BOOST_PARTIAL if match == "state_partial" else 0
        ranked.append({**hit, "_advisory_score": score, "location_match": match})
    return sorted(ranked, key=lambda hit: hit["_advisory_score"], reverse=True)


def location_status(hits: list[dict], requested: bool) -> str:
    if not requested:
        return "not_requested"
    if not hits:
        return "no_match"
    matches = {hit.get("location_match") for hit in hits}
    for match, status in (("matched", "location_matched"), ("state_partial", "location_partial"),
                          ("national", "location_not_applied")):
        if match in matches:
            return status
    return "other_only"
