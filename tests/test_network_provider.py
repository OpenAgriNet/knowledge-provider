import pytest

from pipeline.network_provider import Group, InvalidProviderIdError, ProviderNaming

MAHARASHTRA = Group(code="mh", name="Maharashtra")


def naming(**fields):
    return ProviderNaming(
        **{"shared_instance": "bv", "shared_name": "Bharat Vistaar", "network_id": "provider.oan.dev", **fields}
    )


def _provider(provider_id, name):
    return {"id": provider_id, "descriptor": {"code": provider_id, "name": name}}


class TestProviderPerInstance:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "group,expected",
        [
            (None, _provider("bv.provider.oan.dev", "Bharat Vistaar")),
            (MAHARASHTRA, _provider("mh.provider.oan.dev", "Maharashtra")),
        ],
    )
    def test_one_provider_per_instance_named_after_it(self, group, expected):
        assert naming().provider(group) == expected

    @pytest.mark.unit
    def test_follows_the_network_id(self):
        assert naming(network_id="provider.oan.prod").provider(MAHARASHTRA)["id"] == "mh.provider.oan.prod"

    @pytest.mark.unit
    def test_follows_a_configured_template_and_shared_instance(self):
        configured = naming(shared_instance="portal", shared_name="Portal", id_template="{network_id}-{instance}")

        assert configured.provider(None) == _provider("provider.oan.dev-portal", "Portal")


class TestCatalogNames:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "kind,group,expected",
        [
            ("advisory", None, "Advisories from documents"),
            ("scheme", None, "Schemes from documents"),
            ("advisory", MAHARASHTRA, "Maharashtra Advisories from documents"),
            ("scheme", MAHARASHTRA, "Maharashtra Schemes from documents"),
        ],
    )
    def test_default_names(self, kind, group, expected):
        assert naming().catalog_name(kind, group) == expected

    @pytest.mark.unit
    def test_configured_names(self):
        configured = naming(
            group_catalog_name_template="{catalog_name} ({group_name})",
            catalog_names={"advisory": "Advice", "scheme": "Schemes"},
        )

        assert configured.catalog_name("advisory", MAHARASHTRA) == "Advice (Maharashtra)"
        assert configured.catalog_name("scheme", None) == "Schemes"


class TestProviderIdOverrides:
    NAMING = naming(provider_id_overrides={"ka": "ka.example.com", "bv": "bharat.example"})

    @pytest.mark.unit
    def test_overridden_instance_uses_its_own_id(self):
        provider = self.NAMING.provider(Group(code="ka", name="Karnataka"))

        assert provider == _provider("ka.example.com", "Karnataka")

    @pytest.mark.unit
    def test_instance_without_an_override_uses_the_template(self):
        assert self.NAMING.provider(MAHARASHTRA)["id"] == "mh.provider.oan.dev"

    @pytest.mark.unit
    def test_shared_instance_can_be_overridden(self):
        assert self.NAMING.provider(None)["id"] == "bharat.example"


class TestInvalidProviderIdAtPublish:
    @pytest.mark.unit
    @pytest.mark.parametrize("code", ["ladakh_ut", "u p", "MH."])
    def test_an_instance_that_renders_an_invalid_id_is_refused(self, code):
        with pytest.raises(InvalidProviderIdError, match="must be lowercase"):
            naming().provider(Group(code=code, name="Somewhere"))

    @pytest.mark.unit
    def test_an_invalid_override_is_refused(self):
        with pytest.raises(InvalidProviderIdError):
            naming(provider_id_overrides={"ka": "KA.example"}).provider(Group(code="ka", name="Karnataka"))
