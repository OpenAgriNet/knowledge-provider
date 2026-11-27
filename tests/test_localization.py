import pytest
from fastapi import HTTPException
from qdrant_client import QdrantClient

from pipeline import db
from pipeline.api.routers.search import run_search
from pipeline.auth.models import AuthUser
from pipeline.metadata.localization import (
    localize_search_hit,
    normalize_language_tag,
    project_chunk_localization,
)
from pipeline.vector_store import qdrant_store

SOURCE = "કપાસને યોગ્ય સમયે પાણી આપો."
ENGLISH = "Water cotton at the right time."
HINDI = "कपास को सही समय पर पानी दें।"


def _page():
    return {
        "page_number": 1, "detected_language": "gu", "original_markdown": f"<p>{SOURCE}</p>",
        "translations": {
            "en": {"translated_markdown": ENGLISH, "is_reviewed": True},
            "hi": {"translated_markdown": HINDI, "is_reviewed": False},
        },
    }


def _chunk(text):
    return {
        "original_text": text, "chunking_provider": "recursive_splitter",
        "source_spans_json": [{"page_number": 1, "start_char": 0, "end_char": len(text)}],
    }


def test_full_page_translation_keeps_source_and_coordinate_basis():
    projected = project_chunk_localization(_chunk(SOURCE), [_page()], source_first=True)

    assert projected["primary_language"] == projected["text_language"] == "gu"
    assert projected["primary_text"] == SOURCE
    assert projected["translations"] == {
        "en": {"text": ENGLISH, "is_reviewed": True},
        "hi": {"text": HINDI, "is_reviewed": False},
    }
    assert projected["source_evidence"] == [{
        "page_number": 1, "source_language": "gu", "translation_languages": ["en", "hi"],
        "span_basis": "normalized_source_text", "spans": [{"start_char": 0, "end_char": len(SOURCE)}],
    }]


def test_partial_page_passage_does_not_claim_whole_page_translation():
    source = "કપાસને યોગ્ય સમયે"
    projected = project_chunk_localization(_chunk(source), [_page()], source_first=True)
    hit = {"text": source, **projected}

    assert projected["translations"] == {}
    assert projected["source_evidence"][0]["translation_languages"] == ["en", "hi"]
    assert localize_search_hit(hit, "en", strict=True) is None
    fallback = localize_search_hit(hit, "en")
    assert fallback["text"] == source
    assert fallback["text_language"] == "gu"
    assert fallback["language_fallback"] is True


def test_language_tag_handles_script_region_extensions_and_rejects_ranges():
    assert normalize_language_tag("sr-Latn-RS-u-nu-latn") == "sr-latn-rs-u-nu-latn"
    assert normalize_language_tag("fr-CA,en") is None
    assert normalize_language_tag("*") is None
    assert normalize_language_tag("hi_IN") is None


def test_older_passage_labels_are_preserved_without_language_specific_payload_fields(monkeypatch):
    monkeypatch.setenv("EMBEDDING_VECTOR_SIZE", "2")
    monkeypatch.setattr(qdrant_store, "embed_passages", lambda passages, **_: [[1.0, 0.0] for _ in passages])
    store = qdrant_store.QdrantVectorStore(client=QdrantClient(":memory:"))
    store.upsert("older-labels", [{
        "doc_id": "advisory", "text": SOURCE, "name": "Source title", "doc_language": "gu",
        "title_gu": "જૂનું શીર્ષક", "name_hi": "स्थानीय शीर्षक", "title_sr_Latn_RS": "Naslov",
        "name_en": "English name", "title_en": "English title",
        "localized_labels": {"gu": {"title": "સાચવેલું શીર્ષક"}},
        "instance": "bv", "instance_name": "Bv", "provider_id": "bv.example.org",
        "pop_id": "old-pop", "season": "old-label", "category_tags": "crop",
        "doc_short_description": "Old response alias", "quality_score": 0,
    }])
    payload, = store.list_by_doc_id("older-labels", "advisory", raw_payload=True)
    assert payload["localized_labels"] == {
        "gu": {"title": "સાચવેલું શીર્ષક"}, "hi": {"title": "स्थानीय शीर्षक"},
        "sr-latn-rs": {"title": "Naslov"}, "en": {"title": "English title"},
    }
    assert payload["primary_language"] == "gu"
    assert payload["name"] == "Source title"
    assert payload["provider_id"] == "bv.example.org" and payload["instance"] == "bv"
    assert not {"name_en", "name_hi", "title_en", "title_gu", "title_sr_Latn_RS",
                "doc_language", "instance_name", "pop_id", "season", "category_tags",
                "doc_short_description", "quality_score"}.intersection(payload)


@pytest.mark.asyncio
async def test_search_localizes_one_vector_and_filters_exact_language_before_ranking(monkeypatch):
    indexed_inputs = []

    def embed(passages, **kwargs):
        indexed_inputs.extend(passages)
        return [[1.0, 0.0] if "English only" in passage else [0.5, 0.5] for passage in passages]

    monkeypatch.setenv("EMBEDDING_VECTOR_SIZE", "2")
    monkeypatch.setattr(qdrant_store, "embed_passages", embed)
    monkeypatch.setattr(qdrant_store, "embed_query", lambda *_, **__: [1.0, 0.0])
    monkeypatch.setattr(db, "get_search_settings", dict)
    monkeypatch.setenv("VECTOR_DB_COLLECTION_NAME", "language-test")
    monkeypatch.setattr(db, "visible_document_hits", lambda hits, **_: hits)
    store = qdrant_store.QdrantVectorStore(client=QdrantClient(":memory:"))
    monkeypatch.setattr("pipeline.vector_store.get_vector_store", lambda: store)
    source = project_chunk_localization(_chunk(SOURCE), [_page()], source_first=True)
    store.upsert("language-test", [
        {"doc_id": "other", "text": "English only", "is_reference": False, "available_languages": ["en"]},
        {
            "doc_id": "advisory", "text": SOURCE, "is_reference": False, **source,
            "name": "મૂળ શીર્ષક", "description": "મૂળ વર્ણન",
            "localized_labels": {"hi": {"title": "स्थानीय शीर्षक", "description": "स्थानीय विवरण"}},
        },
    ])
    assert indexed_inputs == ["English only", SOURCE]
    point, = store.list_by_doc_id("language-test", "advisory")
    assert point["translations"]["hi"]["text"] == HINDI
    assert point["primary_text"] == SOURCE
    assert point["name"] == "મૂળ શીર્ષક" and "name_en" not in point

    user = AuthUser(user_id="test-user", roles=["superadmin"])
    search = {"query": "cotton", "index_name": "language-test", "top_k": 1, "candidate_cap": 1}
    from pipeline.search.models import SearchRequest

    strict_hit, = (await run_search(SearchRequest(**search, language="hi", strict_language=True), user))["hits"]
    assert strict_hit["text"] == HINDI
    assert strict_hit["text_language"] == "hi"
    assert strict_hit["primary_language"] == "gu"
    assert strict_hit["language_match"] == "exact"
    assert strict_hit["language_fallback"] is False
    assert strict_hit["text_is_reviewed"] is False
    assert strict_hit["name"] == "स्थानीय शीर्षक"
    assert strict_hit["description"] == "स्थानीय विवरण"
    assert strict_hit["name_en"] == "મૂળ શીર્ષક"

    lookup = await run_search(SearchRequest(**{**search, "candidate_cap": 2, "top_k": 2, "language": "en-IN"}), user)
    english = next(hit for hit in lookup["hits"] if hit["doc_id"] == "advisory")
    assert english["text"] == ENGLISH
    assert english["text_language"] == "en"
    assert english["language_match"] == "lookup"
    assert english["language_fallback"] is True

    with pytest.raises(HTTPException) as error:
        await run_search(SearchRequest(**search, language="en;q=1"), user)
    assert error.value.status_code == 422


def test_qdrant_inspects_one_chunk_without_search_aliases(monkeypatch):
    monkeypatch.setenv("EMBEDDING_VECTOR_SIZE", "2")
    monkeypatch.setattr(qdrant_store, "embed_passages", lambda passages, **_: [[1.0, 0.0] for _ in passages])
    store = qdrant_store.QdrantVectorStore(client=QdrantClient(":memory:"))
    store.upsert("chunk-payloads", [
        {
            "doc_id": "example", "chunk_num": 1, "text": "First paragraph",
            "document_tags": ["field notes"], "chunk_tags": ["planting"],
            "subjects": [{"type": "crop", "name": "Wheat"}],
        },
        {"doc_id": "example", "chunk_num": 2, "text": "Second paragraph"},
        {"doc_id": "other", "chunk_num": 1, "text": "Another document"},
    ])

    payload, = store.list_by_doc_id("chunk-payloads", "example", chunk_num=1, raw_payload=True)
    assert payload["chunk_num"] == 1 and payload["doc_id"] == "example"
    assert payload["document_tags"] == ["field notes"]
    assert payload["chunk_tags"] == ["planting"]
    assert payload["subjects"] == [{"type": "crop", "name": "Wheat"}]
    assert not {"crop_subjects", "chunk_number", "_id", "_score"}.intersection(payload)
    assert store.list_by_doc_id("chunk-payloads", "example", chunk_num=3, raw_payload=True) == []
