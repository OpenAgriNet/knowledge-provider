import json
from pathlib import Path

import pytest

from pipeline.metadata.intake_profiles import (
    IntakeProfileConfigurationError,
    apply_intake_profile,
    load_intake_profiles,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "test_data" / "intake-metadata-profiles.example.yaml"


@pytest.mark.api
@pytest.mark.db
def test_upload_uses_configured_schema_and_persists_the_pin(test_client, db_connection, sample_pdf_content, monkeypatch):
    monkeypatch.setenv("INTAKE_METADATA_PROFILES", str(EXAMPLE))
    options = test_client.get("/upload/options").json()
    crop = next(item for item in options["metadata_profiles"] if item["subject_category"] == "Crop")
    metadata = {
        "subject_categories": ["Crop"], "attribute_profile": crop["pin"],
        "attributes": {"sowing_days": 32, "variety": "Durum", "notes": {"field": "east"}}, "tags": ["Dry land"],
    }

    def upload(values):
        return test_client.post(
            "/upload", params={"kind": "advisory"}, data={"knowledge_metadata": json.dumps(values)},
            files={"file": ("crop-profile.pdf", sample_pdf_content, "application/pdf")},
        )

    assert upload({**metadata, "attributes": {"sowing_days": "32"}}).status_code == 422
    stale = upload({**metadata, "attribute_profile": {**crop["pin"], "digest": "a" * 64}})
    assert stale.status_code == 422
    assert "Reload" in stale.json()["detail"]
    accepted = upload(metadata)
    assert accepted.status_code == 200, accepted.text
    assert db_connection.get_document(accepted.json()["workflow_id"])["knowledge_metadata"] == metadata


def test_profile_selection_and_offline_schema_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("INTAKE_METADATA_PROFILES", str(EXAMPLE))
    result = apply_intake_profile("advisory", {
        "subject_categories": ["Livestock"], "attributes": {"age_weeks": 3, "custom": "dairy"},
    })
    assert result["attribute_profile"]["id"] == "livestock-advisory"
    assert apply_intake_profile("video", None) is None

    with pytest.raises(ValueError, match="Choose one"):
        apply_intake_profile("advisory", {"subject_categories": ["Crop", "Livestock"]})

    invalid = tmp_path / "remote.json"
    invalid.write_text(json.dumps({"profiles": [{
        "id": "unsafe", "version": "1", "kind": "advisory",
        "schema": {"type": "object", "$ref": "https://example.invalid/schema"},
    }]}))
    monkeypatch.setenv("INTAKE_METADATA_PROFILES", str(invalid))
    with pytest.raises(IntakeProfileConfigurationError):
        load_intake_profiles()
