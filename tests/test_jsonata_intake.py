import pytest

from pipeline.intake.webhook_mapping import JsonataMapping, map_source_submission


def test_jsonata_maps_and_filters_nested_advisories():
    mapping = JsonataMapping(
        runtime="jsonata-v1",
        expression='items[published=true].{"external_id": $string(id), "title": headline, "text": $trim(body)}',
    )
    rows = map_source_submission(
        {"items": [
            {"id": 7, "published": True, "headline": "Advisory", "body": " Water before noon. "},
            {"id": 8, "published": False, "headline": "Draft", "body": "Do not serve."},
        ]},
        mapping,
    )
    evidence, document = rows[0]
    assert len(rows) == 1
    assert document.external_id == "7"
    assert document.text == "Water before noon."
    assert len(evidence["source_payload_sha256"]) == 64


def test_jsonata_canonical_validation_rejects_missing_text():
    with pytest.raises(ValueError):
        map_source_submission({}, JsonataMapping(runtime="jsonata-v1", expression='{"external_id": "x"}'))
