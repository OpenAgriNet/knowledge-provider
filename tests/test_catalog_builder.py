"""Unit tests for the Network Catalog Envelope's catalog builder."""

import pytest

from pipeline.catalog_builder import (
    Group,
    ProviderNaming,
    build_catalog,
    is_publishable_kind,
    normalize_document_kind,
)
from pipeline.network_constants import (
    ADVISORY_CATALOG_ID,
    ADVISORY_RESOURCE_ID,
    SCHEMES_CATALOG_ID,
    SCHEMES_RESOURCE_ID,
)

BV_ADVISORY_PROVIDER = {
    "id": "advisory.bv.provider.oan.dev",
    "descriptor": {"code": "advisory.bv.provider.oan.dev", "name": "Advisories from documents"},
}
BV_SCHEMES_PROVIDER = {
    "id": "scheme.bv.provider.oan.dev",
    "descriptor": {"code": "scheme.bv.provider.oan.dev", "name": "Schemes from documents"},
}

# Forbidden by the KnowledgeAdvisory schema under informationMode OnDemand.
# Both advisory and scheme resources use this schema.
FORBIDDEN_ADVISORY_ATTRIBUTES = (
    "issuedAt",
    "validity",
    "recommendations",
    "supportingResourceIds",
    "rationale",
    "source",
)


def _attributes(kind):
    return build_catalog(kind)["resources"][0]["resourceAttributes"]


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
        catalog = build_catalog("advisory")

        assert catalog["id"] == ADVISORY_CATALOG_ID
        assert "bppId" not in catalog
        assert "bppUri" not in catalog
        assert catalog["descriptor"] == {"name": "Agricultural advisory from documents"}
        assert catalog["isActive"] is True
        assert catalog["provider"] == BV_ADVISORY_PROVIDER

    @pytest.mark.unit
    def test_exactly_one_resource_with_a_stable_id(self):
        catalog = build_catalog("advisory")

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
        assert attributes["languages"] == ["en"]

    @pytest.mark.unit
    @pytest.mark.parametrize("field", FORBIDDEN_ADVISORY_ATTRIBUTES)
    def test_omits_fields_forbidden_under_on_demand(self, field):
        assert field not in _attributes("advisory")


class TestBuildSchemeCatalog:
    @pytest.mark.unit
    def test_catalog_envelope_fields(self):
        catalog = build_catalog("scheme")

        assert catalog["id"] == SCHEMES_CATALOG_ID
        assert "bppId" not in catalog
        assert "bppUri" not in catalog
        assert catalog["descriptor"] == {"name": "Schemes from documents"}
        assert catalog["isActive"] is True
        assert catalog["provider"] == BV_SCHEMES_PROVIDER

    @pytest.mark.unit
    def test_exactly_one_resource_with_a_stable_id(self):
        catalog = build_catalog("scheme")

        assert len(catalog["resources"]) == 1
        resource = catalog["resources"][0]
        assert resource["id"] == SCHEMES_RESOURCE_ID
        assert resource["descriptor"] == {"name": "Government schemes"}

    @pytest.mark.unit
    def test_typed_as_knowledge_advisory_distinguished_by_subject_categories(self):
        # No KnowledgeScheme schema exists - a scheme shares KnowledgeAdvisory
        # with the advisory resource, distinguished only by subjectCategories.
        attributes = _attributes("scheme")

        assert attributes["@type"] == "openagrinet:KnowledgeAdvisory"
        assert attributes["@context"].endswith("/KnowledgeAdvisory/v0.1/context.jsonld")
        assert attributes["subjectCategories"] == ["Scheme"]
        assert attributes["informationMode"] == "OnDemand"
        assert attributes["topics"] == ["Government schemes"]
        assert attributes["languages"] == ["en"]

    @pytest.mark.unit
    @pytest.mark.parametrize("field", FORBIDDEN_ADVISORY_ATTRIBUTES)
    def test_omits_fields_forbidden_under_on_demand(self, field):
        assert field not in _attributes("scheme")


class TestUnmappedKinds:
    @pytest.mark.unit
    @pytest.mark.parametrize("kind", ["document", "video", "how_to_faq", "", None])
    def test_returns_none_so_the_caller_skips_publishing(self, kind):
        assert build_catalog(kind) is None
        assert is_publishable_kind(kind) is False

    @pytest.mark.unit
    @pytest.mark.parametrize("kind", ["advisory", " Scheme "])
    def test_knowledge_kinds_are_publishable(self, kind):
        assert is_publishable_kind(kind) is True


class TestBuilderIsolation:
    @pytest.mark.unit
    def test_mutating_a_built_catalog_does_not_leak_into_the_next(self):
        first = build_catalog("advisory")
        first["resources"][0]["resourceAttributes"]["topics"].append("tampered")

        second = build_catalog("advisory")

        assert second["resources"][0]["resourceAttributes"]["topics"] == ["Crop production"]

    @pytest.mark.unit
    def test_mutating_a_built_catalogs_provider_does_not_leak_into_the_next(self):
        first = build_catalog("advisory")
        first["provider"]["descriptor"]["name"] = "tampered"

        second = build_catalog("advisory")

        assert second["provider"] == BV_ADVISORY_PROVIDER


MAHARASHTRA = Group(code="mh", name="Maharashtra")


class TestGroupProviderCatalog:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "kind,catalog_id,resource_id,provider_id,name",
        [
            (
                "advisory",
                "mh-cat-oan-knowledge-provider-advisories",
                "mh-res-oan-knowledge-provider-advisories",
                "advisory.mh.provider.oan.dev",
                "Maharashtra Advisories from documents",
            ),
            (
                "scheme",
                "mh-cat-oan-knowledge-provider-schemes",
                "mh-res-oan-knowledge-provider-schemes",
                "scheme.mh.provider.oan.dev",
                "Maharashtra Schemes from documents",
            ),
        ],
    )
    def test_ids_and_names_carry_the_group(self, kind, catalog_id, resource_id, provider_id, name):
        catalog = build_catalog(kind, MAHARASHTRA)

        assert catalog["id"] == catalog_id
        assert catalog["descriptor"] == {"name": name}
        assert catalog["resources"][0]["id"] == resource_id
        assert catalog["provider"] == {
            "id": provider_id,
            "descriptor": {"code": provider_id, "name": name},
        }

    @pytest.mark.unit
    @pytest.mark.parametrize("kind", ["advisory", "scheme"])
    def test_resource_attributes_match_the_shared_provider(self, kind):
        grouped = build_catalog(kind, MAHARASHTRA)["resources"][0]
        shared = build_catalog(kind)["resources"][0]

        assert grouped["resourceAttributes"] == shared["resourceAttributes"]
        assert grouped["descriptor"] == shared["descriptor"]

    @pytest.mark.unit
    def test_unmapped_kind_still_publishes_nothing(self):
        assert build_catalog("document", MAHARASHTRA) is None

    @pytest.mark.unit
    def test_no_group_uses_the_shared_instance(self):
        catalog = build_catalog("advisory", None)

        assert catalog["id"] == ADVISORY_CATALOG_ID
        assert catalog["provider"] == BV_ADVISORY_PROVIDER


class TestProviderNaming:
    NAMING = ProviderNaming(
        shared_instance="portal",
        id_template="{instance}-{kind}.example.net",
        group_name_template="{provider_name} ({group_name})",
        provider_names={"advisory": "Advice", "scheme": "Schemes"},
    )

    @pytest.mark.unit
    def test_shared_provider_follows_the_configured_naming(self):
        catalog = build_catalog("scheme", None, self.NAMING)

        assert catalog["id"] == SCHEMES_CATALOG_ID
        assert catalog["provider"] == {
            "id": "portal-scheme.example.net",
            "descriptor": {"code": "portal-scheme.example.net", "name": "Schemes"},
        }

    @pytest.mark.unit
    def test_group_provider_follows_the_configured_naming(self):
        catalog = build_catalog("advisory", MAHARASHTRA, self.NAMING)

        assert catalog["descriptor"] == {"name": "Advice (Maharashtra)"}
        assert catalog["provider"] == {
            "id": "mh-advisory.example.net",
            "descriptor": {"code": "mh-advisory.example.net", "name": "Advice (Maharashtra)"},
        }
