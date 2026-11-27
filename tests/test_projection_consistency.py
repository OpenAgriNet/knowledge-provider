"""Saved reviewer corrections must agree with returned facts and filter keys."""

import pytest

from pipeline.metadata.extraction import MetadataFilters, source_digest
from pipeline.metadata.knowledge_metadata import KnowledgeMetadata
from pipeline.metadata.metadata_edits import EditableFact, supplied_facts
from pipeline.search.passage_projection import _prepare_records

CHUNK = {"chunk_number": 1, "original_text": "Wheat and Rice guidance."}
SUBJECT = {"type": "crop", "subjectId": "urn:crop:wheat", "descriptor": {"name": "Wheat"}, "attributes": {"sowingDays": 30}}


def review(crop):
    return {"accepted": {"source_digest": source_digest([(1, CHUNK["original_text"])]), "overrides": ["crop"],
                         "facts": [{"field": "crop", "value": crop}] if crop else []}}


@pytest.mark.parametrize("chunk_scope", [False, True])
def test_crop_correction_removes_old_identity_and_attributes(chunk_scope):
    record, = _prepare_records("doc", "file.pdf", [{**CHUNK, **({"metadata_review": review("Rice")} if chunk_scope else {})}],
                               metadata_review={} if chunk_scope else review("Rice"),
                               knowledge_metadata={"subject_categories": ["Crop"], "subjects": [SUBJECT]})
    assert record["crop_keys"] == ["rice"]
    assert record["subjects"] == [{"type": "crop", "descriptor": {"name": "Rice"}}]
    assert "metadata" not in record
    assert "agriculture_subject_ids" not in record


def test_clear_crop_removes_its_fields_but_keeps_other_subject_types():
    livestock = {"type": "livestock", "descriptor": {"name": "Cattle"}}
    record, = _prepare_records("doc", "file.pdf", [CHUNK], metadata_review=review(None),
                               knowledge_metadata={"subjects": [SUBJECT, livestock]})
    assert record["subjects"] == [livestock]
    assert "metadata" not in record and "crop_keys" not in record


def test_same_crop_retains_its_attributes_and_identifier():
    record, = _prepare_records("doc", "file.pdf", [CHUNK], metadata_review=review("Wheat"),
                               knowledge_metadata={"subjects": [SUBJECT]})
    assert record["agriculture_subject_ids"] == ["urn:crop:wheat"]
    assert record["metadata"]["subjects"] == [SUBJECT]


def test_untyped_crop_has_consistent_filter_without_inventing_a_type():
    metadata = {"subject_categories": ["Crop"], "agriculture_subjects": [{"descriptor": {"name": "Rice"}}]}
    record, = _prepare_records("doc", "file.pdf", [CHUNK], knowledge_metadata=metadata)
    assert record["crop_keys"] == ["rice"]
    assert record["subjects"] == metadata["agriculture_subjects"]
    assert {"field": "crop", "value": "Rice"} in supplied_facts({"knowledge_metadata": metadata})


def test_category_contract_is_shared_when_no_enum_is_installed(monkeypatch):
    monkeypatch.delenv("INTAKE_SUBJECT_SCHEMA", raising=False)
    value = "Incident response"
    assert KnowledgeMetadata(subject_categories=[value]).subject_categories == [value]
    assert EditableFact(field="category", value=value).value == value
    assert MetadataFilters(category=value).qdrant_values()["category"] == value
    assert supplied_facts({"knowledge_metadata": {"subject_categories": [value]}}) == [{"field": "category", "value": value}]


def test_installed_enum_applies_at_intake_without_invalidating_saved_data(tmp_path, monkeypatch):
    from pipeline.metadata.intake_profiles import apply_intake_profile
    schema = tmp_path / "schema.json"
    schema.write_text('{"properties":{"subjectCategories":{"items":{"enum":["Incident response"]}}}}')
    monkeypatch.setenv("INTAKE_SUBJECT_SCHEMA", str(schema))
    with pytest.raises(ValueError, match="installed schema"):
        apply_intake_profile("advisory", {"subject_categories": ["Other"]})
    assert KnowledgeMetadata(subject_categories=["Other"]).subject_categories == ["Other"]
    assert EditableFact(field="category", value="Other").value == "Other"
    assert MetadataFilters(category="Other").category == "Other"
