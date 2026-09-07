"""Unit tests for the Network Catalog Envelope's catalog builder."""

import pytest

from pipeline.network_catalog import (
    ADVISORY_CATALOG_ID,
    ADVISORY_RESOURCE_ID,
    SCHEMES_CATALOG_ID,
    SCHEMES_RESOURCE_ID,
    build_catalog,
    normalize_document_kind,
)

BPP_ID = "docs-pipeline-bv"
BPP_URI = "https://docs.example.gov.in"

# Forbidden by the schema when informationMode is OnDemand: the provider is
# saying "ask me", so it must not also ship the answer or its provenance.
FORBIDDEN_ADVISORY_ATTRIBUTES = (
    "issuedAt",
    "validity",
    "recommendations",
    "supportingResourceIds",
    "rationale",
    "source",
)
FORBIDDEN_RESOURCE_ATTRIBUTES = (
    "knowledgeType",
    "version",
    "lifecycleStatus",
    "content",
    "validity",
    "provenance",
    "supersedes",
)


def _attributes(kind):
    return build_catalog(kind, BPP_ID, BPP_URI)["resources"][0]["resourceAttributes"]


class TestNormalizeDocumentKind:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("advisory", "advisory"),
            ("  Advisory  ", "advisory"),
            ("SCHEME", "scheme"),
            ("", "document"),
            (None, "document"),
        ],
    )
    def test_normalizes_to_lookup_form(self, raw, expected):
        assert normalize_document_kind(raw) == expected


class TestBuildAdvisoryCatalog:
    @pytest.mark.unit
    def test_catalog_envelope_fields(self):
        catalog = build_catalog("advisory", BPP_ID, BPP_URI)

        assert catalog["id"] == ADVISORY_CATALOG_ID
        assert catalog["bppId"] == BPP_ID
        assert catalog["bppUri"] == BPP_URI
        assert catalog["descriptor"] == {"name": "Agricultural advisory from documents"}
        assert catalog["isActive"] is True

    @pytest.mark.unit
    def test_exactly_one_resource_with_a_stable_id(self):
        catalog = build_catalog("advisory", BPP_ID, BPP_URI)

        assert len(catalog["resources"]) == 1
        resource = catalog["resources"][0]
        assert resource["id"] == ADVISORY_RESOURCE_ID
        assert resource["descriptor"] == {"name": "Agricultural advisory"}

    @pytest.mark.unit
    def test_resource_attributes(self):
        attributes = _attributes("advisory")

        assert attributes["@type"] == "openagrinet:KnowledgeAdvisory"
        assert attributes["@context"].endswith("/KnowledgeAdvisory/v0.1/context.jsonld")
        assert attributes["informationMode"] == "OnDemand"
        assert attributes["subjectCategories"] == ["Crop"]
        assert attributes["topics"] == ["Crop production"]
        assert attributes["languages"] == ["en", "hi", "mr"]

    @pytest.mark.unit
    @pytest.mark.parametrize("field", FORBIDDEN_ADVISORY_ATTRIBUTES)
    def test_omits_fields_forbidden_under_on_demand(self, field):
        assert field not in _attributes("advisory")


class TestBuildSchemeCatalog:
    @pytest.mark.unit
    def test_catalog_envelope_fields(self):
        catalog = build_catalog("scheme", BPP_ID, BPP_URI)

        assert catalog["id"] == SCHEMES_CATALOG_ID
        assert catalog["bppId"] == BPP_ID
        assert catalog["bppUri"] == BPP_URI
        assert catalog["descriptor"] == {"name": "Schemes from documents"}
        assert catalog["isActive"] is True

    @pytest.mark.unit
    def test_exactly_one_resource_with_a_stable_id(self):
        catalog = build_catalog("scheme", BPP_ID, BPP_URI)

        assert len(catalog["resources"]) == 1
        resource = catalog["resources"][0]
        assert resource["id"] == SCHEMES_RESOURCE_ID
        assert resource["descriptor"] == {"name": "Government schemes"}

    @pytest.mark.unit
    def test_typed_as_knowledge_resource_not_advisory(self):
        attributes = _attributes("scheme")

        assert attributes["@type"] == "openagrinet:KnowledgeResource"
        assert attributes["@context"].endswith("/KnowledgeResource/v0.1/context.jsonld")
        assert attributes["subjectCategories"] == ["Scheme"]

    @pytest.mark.unit
    def test_declares_supported_knowledge_types(self):
        # Required for KnowledgeResource under OnDemand; KnowledgeAdvisory has
        # no equivalent, which is why the two kinds do not share attributes.
        attributes = _attributes("scheme")

        assert attributes["supportedKnowledgeTypes"] == ["Reference"]
        assert attributes["informationMode"] == "OnDemand"
        assert attributes["topics"] == ["Government schemes"]
        assert attributes["languages"] == ["en", "hi", "mr"]

    @pytest.mark.unit
    @pytest.mark.parametrize("field", FORBIDDEN_RESOURCE_ATTRIBUTES)
    def test_omits_fields_forbidden_under_on_demand(self, field):
        assert field not in _attributes("scheme")


class TestUnmappedKinds:
    @pytest.mark.unit
    @pytest.mark.parametrize("kind", ["document", "video", "how_to_faq", "", None])
    def test_returns_none_so_the_caller_skips_publishing(self, kind):
        assert build_catalog(kind, BPP_ID, BPP_URI) is None


class TestBuilderIsolation:
    @pytest.mark.unit
    def test_mutating_a_built_catalog_does_not_leak_into_the_next(self):
        first = build_catalog("advisory", BPP_ID, BPP_URI)
        first["resources"][0]["resourceAttributes"]["topics"].append("tampered")

        second = build_catalog("advisory", BPP_ID, BPP_URI)

        assert second["resources"][0]["resourceAttributes"]["topics"] == ["Crop production"]
