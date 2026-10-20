import pytest

from pipeline.network_provider import Group, ProviderNaming

MAHARASHTRA = Group(code="mh", name="Maharashtra")


def _provider(provider_id, name):
    return {"id": provider_id, "descriptor": {"code": provider_id, "name": name}}


class TestDefaultProviderNaming:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "kind,group,expected",
        [
            ("advisory", None, _provider("advisory.bv.provider.oan.dev", "Advisories from documents")),
            ("scheme", None, _provider("scheme.bv.provider.oan.dev", "Schemes from documents")),
            (
                "advisory",
                MAHARASHTRA,
                _provider("advisory.mh.provider.oan.dev", "Maharashtra Advisories from documents"),
            ),
            (
                "scheme",
                MAHARASHTRA,
                _provider("scheme.mh.provider.oan.dev", "Maharashtra Schemes from documents"),
            ),
        ],
    )
    def test_provider_per_kind_and_instance(self, kind, group, expected):
        assert ProviderNaming(shared_instance="bv").provider(kind, group) == expected


class TestConfiguredProviderNaming:
    NAMING = ProviderNaming(
        shared_instance="portal",
        id_template="{instance}-{kind}.example.net",
        group_name_template="{provider_name} ({group_name})",
        provider_names={"advisory": "Advice", "scheme": "Schemes"},
    )

    @pytest.mark.unit
    def test_shared_provider_follows_the_configured_naming(self):
        assert self.NAMING.provider("scheme", None) == _provider("portal-scheme.example.net", "Schemes")

    @pytest.mark.unit
    def test_group_provider_follows_the_configured_naming(self):
        assert self.NAMING.provider("advisory", MAHARASHTRA) == _provider(
            "mh-advisory.example.net", "Advice (Maharashtra)"
        )


class TestProviderIdOverrides:
    NAMING = ProviderNaming(
        shared_instance="bv",
        provider_id_overrides={
            "ka": {"advisory": "advisory.ka.com", "scheme": "scheme.ka.com"},
            "tn": {"scheme": "scheme.tn.com"},
            "bv": {"advisory": "advisory.bharat.example"},
        }
    )

    @pytest.mark.unit
    @pytest.mark.parametrize("kind,provider_id", [("advisory", "advisory.ka.com"), ("scheme", "scheme.ka.com")])
    def test_overridden_group_uses_its_own_provider_id(self, kind, provider_id):
        provider = self.NAMING.provider(kind, Group(code="ka", name="Karnataka"))

        assert provider["id"] == provider_id
        assert provider["descriptor"]["code"] == provider_id

    @pytest.mark.unit
    def test_override_changes_only_the_id(self):
        provider = self.NAMING.provider("scheme", Group(code="ka", name="Karnataka"))

        assert provider["descriptor"]["name"] == "Karnataka Schemes from documents"

    @pytest.mark.unit
    def test_a_kind_without_an_override_falls_back_to_the_template(self):
        provider = self.NAMING.provider("advisory", Group(code="tn", name="Tamil Nadu"))

        assert provider["id"] == "advisory.tn.provider.oan.dev"

    @pytest.mark.unit
    def test_group_without_overrides_uses_the_defaults(self):
        assert self.NAMING.provider("scheme", MAHARASHTRA) == _provider(
            "scheme.mh.provider.oan.dev", "Maharashtra Schemes from documents"
        )

    @pytest.mark.unit
    def test_shared_instance_can_be_overridden(self):
        assert self.NAMING.provider("advisory", None)["id"] == "advisory.bharat.example"
