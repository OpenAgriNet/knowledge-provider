"""Language and source-evidence fields for published passages."""

import json
import re

# RFC 5646 syntax. Tags are compared case-insensitively and stored lowercase.
_LANGUAGE_TAG = re.compile(
    r"(?:(?:[a-z]{2,3}(?:-[a-z]{3}){0,3}|[a-z]{4,8})"
    r"(?:-[a-z]{4})?(?:-(?:[a-z]{2}|[0-9]{3}))?"
    r"(?:-(?:[a-z0-9]{5,8}|[0-9][a-z0-9]{3}))*"
    r"(?:-[a-wy-z0-9](?:-[a-z0-9]{2,8})+)*(?:-x(?:-[a-z0-9]{1,8})+)?"
    r"|x(?:-[a-z0-9]{1,8})+)",
    re.ASCII,
)
_GRANDFATHERED_TAGS = frozenset({
    "en-gb-oed", "i-ami", "i-bnn", "i-default", "i-enochian", "i-hak", "i-klingon",
    "i-lux", "i-mingo", "i-navajo", "i-pwn", "i-tao", "i-tay", "i-tsu", "sgn-be-fr",
    "sgn-be-nl", "sgn-ch-de", "art-lojban", "cel-gaulish", "no-bok", "no-nyn", "zh-guoyu",
    "zh-hakka", "zh-min", "zh-min-nan", "zh-xiang",
})


def normalize_language_tag(value: object) -> str | None:
    """Accept one language tag, using a bounded case-insensitive lookup key."""
    if not isinstance(value, str) or len(value) > 255 or not value.isascii():
        return None
    tag = value.strip().lower()
    if tag in _GRANDFATHERED_TAGS or _LANGUAGE_TAG.fullmatch(tag):
        return tag
    return None


def _source_spans(chunk: dict) -> list[dict]:
    spans = chunk.get("source_spans_json") or []
    if isinstance(spans, str):
        spans = json.loads(spans)
    return [
        span for span in spans
        if isinstance(span, dict) and type(span.get("page_number")) is int
    ]


def _page_variants(page: dict, language: str, text: str) -> dict:
    variants = {language: {"text": text, "is_reviewed": bool(page.get("is_reviewed"))}}
    for key, translation in (page.get("translations") or {}).items():
        tag = normalize_language_tag(key)
        content = translation.get("edited_translation") or translation.get("translated_markdown") or ""
        if tag and tag not in variants and content.strip():
            variants[tag] = {"text": content.strip(), "is_reviewed": bool(translation.get("is_reviewed"))}
    return variants


def _matched_spans(spans: list[dict], page_texts: dict[int, str], text: str) -> list[dict]:
    excerpts = []
    for span in spans:
        page_text = page_texts.get(span["page_number"], "")
        start, end = span.get("start_char"), span.get("end_char")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(page_text):
            return []
        excerpts.append(page_text[start:end])
    return spans if " ".join(excerpts).split() == text.split() else []


def project_chunk_localization(chunk: dict, pages: list[dict], *, source_first: bool = False) -> dict:
    """Project language metadata from a chunk created from the source pages.

    Page translations carry page alignment only. A chunk has a complete
    translation when its text covers every cited source page in full.
    """
    if not source_first or not pages:
        return {}

    from pipeline.chunking.recursive_splitter import clean_and_format_text

    spans = _source_spans(chunk)
    page_start = chunk.get("page_start") or 1
    page_end = chunk.get("page_end") or page_start
    numbers = sorted({span["page_number"] for span in spans}) or list(range(page_start, page_end + 1))
    source_pages = {page["page_number"]: page for page in pages if page["page_number"] in numbers}
    primary_text = chunk.get("original_text") or ""
    text = chunk.get("edited_text") or primary_text
    languages = set()
    evidence = []
    comparable_pages = {}
    page_variants = []
    recursive = chunk.get("chunking_provider") == "recursive_splitter"
    for number in numbers:
        page = source_pages.get(number)
        if page is None:
            languages.add("und")
            continue
        language = normalize_language_tag(page.get("detected_language")) or "und"
        languages.add(language)
        page_text = page.get("edited_markdown") or page.get("original_markdown") or ""
        variants = _page_variants(page, language, page_text)
        page_variants.append(variants)
        evidence.append({
            "page_number": number,
            "source_language": language,
            "translation_languages": sorted(variants.keys() - {language}),
        })
        comparable_pages[number] = clean_and_format_text(page_text) if recursive else page_text

    primary_language = next(iter(languages)) if len(languages) == 1 else "mul"
    is_edited = bool(chunk.get("edited_text") and chunk["edited_text"] != chunk.get("original_text"))
    matched_spans = _matched_spans(spans, comparable_pages, primary_text)
    if matched_spans:
        for reference in evidence:
            reference["span_basis"] = "normalized_source_text" if recursive else "source_markdown"
            reference["spans"] = [
                {"start_char": span["start_char"], "end_char": span["end_char"]}
                for span in matched_spans if span["page_number"] == reference["page_number"]
            ]
    translations = {}
    covers_full_pages = (
        len(page_variants) == len(numbers)
        and bool(text.strip())
        and text.split() == " ".join(comparable_pages.values()).split()
    )
    if covers_full_pages:
        shared_languages = set(page_variants[0]).intersection(*page_variants[1:])
        for tag in sorted(shared_languages - {primary_language, "und", "mul"}):
            translations[tag] = {
                "text": "\n\n".join(variants[tag]["text"] for variants in page_variants),
                "is_reviewed": all(variants[tag]["is_reviewed"] for variants in page_variants),
            }

    text_language = "und" if is_edited and not covers_full_pages else primary_language
    return {
        "primary_language": primary_language,
        "text_language": text_language,
        "primary_text": primary_text,
        "translations": translations,
        "source_evidence": evidence,
        "available_languages": sorted({text_language, primary_language, *translations}),
    }


def localize_search_hit(hit: dict, language: str, *, strict: bool = False) -> dict | None:
    """Resolve exact, less-specific, or primary text while reporting its actual language."""
    text_language = normalize_language_tag(hit.get("text_language")) or "und"
    primary_language = normalize_language_tag(hit.get("primary_language")) or text_language
    primary_text = hit.get("primary_text") or hit.get("text") or ""
    variants = {
        **(hit.get("translations") or {}),
        primary_language: {"text": primary_text},
        text_language: {"text": hit.get("text") or ""},
    }
    candidate = language
    while candidate:
        variant = variants.get(candidate)
        if variant:
            return {
                **hit, "text": variant["text"], "text_language": candidate,
                **({"text_is_reviewed": variant["is_reviewed"]} if "is_reviewed" in variant else {}),
                "language_match": "exact" if candidate == language else "lookup",
                "language_fallback": candidate != language,
            }
        if strict:
            return None
        candidate = candidate.rpartition("-")[0]
        if len(candidate.rpartition("-")[2]) == 1:
            candidate = candidate.rpartition("-")[0]
    return {
        **hit, "text": primary_text, "text_language": primary_language,
        "language_match": "primary", "language_fallback": True,
    }
