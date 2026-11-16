import json

import pytest
from qdrant_client import QdrantClient

from pipeline.search.passage_projection import _prepare_records
from pipeline.metadata.knowledge_metadata import KnowledgeFilters, parse_knowledge_metadata
from pipeline.vector_store import qdrant_store


def test_metadata_vocabulary_accepts_labels_from_another_domain():
    fields = {
        "subject_categories": ["Incident response", "incident response"],
        "coverage_areas": [{"codeScheme": "Facility", "areaCode": "B-17", "areaLevel": "Floor"}],
    }

    assert parse_knowledge_metadata(json.dumps(fields)) == {
        **fields, "subject_categories": ["Incident response"],
    }
    assert KnowledgeFilters.model_validate(fields).qdrant_values()["coverage_area_keys"] == ["facility:b-17"]


def test_metadata_retains_typed_applicability_and_bounds_free_tags():
    metadata = parse_knowledge_metadata(json.dumps({
        "subject_categories": ["Livestock"],
        "agriculture_subjects": [{"subjectId": "urn:oan:animal:cattle"}],
        "coverage_areas": [{"codeScheme": "ISO-3166-2", "areaCode": "IN-GJ"}],
        "seasons": ["Kharif"], "tags": ["Water", "Heat", "water"],
        "issued_at": "2026-01-10T08:00:00+05:30",
    }))
    assert metadata["tags"] == ["Water", "Heat"]
    assert metadata["issued_at"] == "2026-01-10T08:00:00+05:30"
    with pytest.raises(ValueError):
        parse_knowledge_metadata(json.dumps({"tags": [str(i) for i in range(17)]}))


def test_vector_search_matches_subject_and_applicability_before_ranking(monkeypatch):
    monkeypatch.setenv("EMBEDDING_VECTOR_SIZE", "2")
    monkeypatch.setattr(qdrant_store, "embed_passages", lambda texts, **kwargs: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(qdrant_store, "embed_query", lambda *args, **kwargs: [1.0, 0.0])
    store = qdrant_store.QdrantVectorStore(client=QdrantClient(":memory:"))
    text = "Provide clean drinking water and monitor cattle daily."
    records = []
    for code, season in [("IN-GJ", "Kharif"), ("IN-MH", "Kharif"), ("IN-GJ", "Rabi"), (None, None), (None, "Broad")]:
        metadata = {
            "subject_categories": ["Livestock"], "agriculture_subjects": [{"subjectId": "urn:oan:animal:cattle"}],
            "coverage_areas": [{"codeScheme": "ISO-3166-2", "areaCode": code}] if code else [],
            "seasons": [season] if season and season != "Broad" else [],
            "broad_applicability": season == "Broad",
        }
        records.extend(_prepare_records(
            f"{code}:{season}", "care.pdf", [{"chunk_number": 1, "original_text": text}],
            kind="advisory", knowledge_metadata=metadata,
        ))
    store.upsert("applicability", records)
    filters = KnowledgeFilters.model_validate({
        "subject_categories": ["Livestock"], "agriculture_subject_ids": ["urn:oan:animal:cattle"],
        "coverage_areas": [{"codeScheme": "iso-3166-2", "areaCode": "in-gj"}], "seasons": ["KHARIF"],
    })
    hits = store.search("applicability", "cattle", applicability=filters.qdrant_values())["hits"]
    assert {hit["doc_id"] for hit in hits} == {"IN-GJ:Kharif", "None:Broad"}


def test_grouped_applicability_keeps_region_season_pairs_together(monkeypatch):
    monkeypatch.setenv("EMBEDDING_VECTOR_SIZE", "2")
    monkeypatch.setattr(qdrant_store, "embed_passages", lambda texts, **kwargs: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(qdrant_store, "embed_query", lambda *args, **kwargs: [1.0, 0.0])
    store = qdrant_store.QdrantVectorStore(client=QdrantClient(":memory:"))

    def area(code):
        return {"codeScheme": "district", "areaCode": code}

    metadata = {
        "applicability_groups": [
            {"coverage_areas": [area("north")], "seasons": ["dry"]},
            {"coverage_areas": [area("south")], "seasons": ["wet"]},
        ],
    }
    records = _prepare_records(
        "grouped", "advice.pdf", [{"chunk_number": 1, "original_text": "Water guidance"}],
        knowledge_metadata=metadata,
    )
    store.upsert("knowledge", records)
    for season, expected in [("dry", {"grouped"}), ("wet", set())]:
        filters = KnowledgeFilters(coverage_areas=[area("north")], seasons=[season])
        hits = store.search("knowledge", "water", applicability=filters.qdrant_values())["hits"]
        assert {hit["doc_id"] for hit in hits} == expected
