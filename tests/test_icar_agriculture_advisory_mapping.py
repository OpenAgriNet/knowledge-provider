"""Exercise the opt-in Direct advisory mapping before deployment."""

from copy import deepcopy
from pathlib import Path

import pytest

from pipeline.intake.webhook_mapping import JsonataMapping, map_source_submission
from tests.icar_webhook_fixture import SORGHUM_PAYLOAD

TEMPLATE = Path(__file__).resolve().parents[1] / "docs/examples/icar-agriculture-advisory/icar-advisory.jsonata"


@pytest.fixture
def direct_mapping():
    expression = TEMPLATE.read_text(encoding="utf-8").replace("__NETWORK_ID__", "intake-test")
    return JsonataMapping(runtime="jsonata-v1", expression=expression.replace("__SCHEMA_DIGEST__", "1" * 64))


@pytest.mark.parametrize(("domain", "commodity", "category"), [
    ("Agriculture", "Sorghum", "Crop"),
    ("Animal Husbandry", "Cattle", "Livestock"),
])
def test_icar_mapping_pins_full_direct_advisory(direct_mapping, domain, commodity, category):
    raw = deepcopy(SORGHUM_PAYLOAD)
    raw.update(domainName=domain, commodityName=commodity, issuedAt="2026-11-01T06:00:00Z", language="en")
    (_, record), = map_source_submission(raw, direct_mapping)
    metadata = record.knowledge_metadata
    pin = metadata.schema_pack
    attributes = pin.attributes
    assert record.kind == "advisory" and record.language == "en"
    assert pin.network_id == "intake-test" and pin.digest == "1" * 64
    assert attributes["@type"] == pin.capability == "openagrinet:AgricultureAdvisory"
    assert attributes["informationMode"] == "Direct"
    assert attributes["issuedAt"] == raw["issuedAt"]
    assert attributes["subjectCategories"] == [category]
    assert attributes["source"]["sourceId"] == record.external_id
    assert attributes["recommendations"][0]["language"] == "en"
    assert attributes["recommendations"][0]["message"]
    if category == "Crop":
        assert attributes["applicability"] == metadata.attributes["applicability"]
    else:
        assert "applicability" not in attributes


@pytest.mark.parametrize("field", ["issuedAt", "language"])
def test_icar_mapping_requires_issuance_and_language_from_the_feed(direct_mapping, field):
    raw = deepcopy(SORGHUM_PAYLOAD)
    raw.update(issuedAt="2026-11-01T06:00:00Z", language="en")
    del raw[field]
    with pytest.raises(ValueError):
        map_source_submission(raw, direct_mapping)
