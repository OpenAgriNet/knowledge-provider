from pathlib import Path

import pytest

from pipeline.network_group import (
    DEFAULT_PROVIDER_CONFIG_OVERRIDE_PATH,
    PROVIDER_CONFIG_OVERRIDE_PATH_ENV,
    NetworkProviderConfigError,
    group_for_instance,
    load_provider_id_overrides,
    load_provider_naming,
    parse_provider_id_overrides,
)


@pytest.fixture(autouse=True)
def _default_no_group_instances(monkeypatch):
    monkeypatch.delenv("PORTAL_INSTANCE", raising=False)
    monkeypatch.delenv("DEFAULT_INSTANCE", raising=False)


class TestGroupForInstance:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "instance,expected",
        [
            ("mh", "mh"),
            ("  MH ", "mh"),
            ("ka", "ka"),
            ("bv", None),
            ("BV", None),
            ("default", None),
            ("", None),
            (None, None),
        ],
    )
    def test_states_are_groups_and_platform_instances_are_not(self, instance, expected):
        assert group_for_instance(instance) == expected

    @pytest.mark.unit
    def test_follows_a_configured_portal_instance(self, monkeypatch):
        monkeypatch.setenv("PORTAL_INSTANCE", "portal")

        assert group_for_instance("portal") is None
        assert group_for_instance("mh") == "mh"

    @pytest.mark.unit
    def test_follows_a_configured_default_instance(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_INSTANCE", "local")

        assert group_for_instance("local") is None
        assert group_for_instance("mh") == "mh"


class TestParseProviderIdOverrides:
    @pytest.mark.unit
    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_unset_means_no_overrides(self, raw):
        assert parse_provider_id_overrides(raw, "providers.json") == {}

    @pytest.mark.unit
    def test_codes_and_kinds_are_case_insensitive_and_ids_trimmed(self):
        parsed = parse_provider_id_overrides(
            '{" KA ": {"Advisory": " advisory.ka.com ", "scheme": "scheme.ka.com"}, "tn": {"scheme": "s.tn"}}',
            "providers.json",
        )

        assert parsed == {
            "ka": {"advisory": "advisory.ka.com", "scheme": "scheme.ka.com"},
            "tn": {"scheme": "s.tn"},
        }

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            '["ka"]',
            '{"ka": "advisory.ka.com"}',
            '{"ka": {}}',
            '{"": {"advisory": "advisory.ka.com"}}',
            '{"ka": {"video": "video.ka.com"}}',
            '{"ka": {"advisory": ""}}',
            '{"ka": {"advisory": 42}}',
            '{"KA": {"advisory": "a"}, "ka": {"advisory": "b"}}',
        ],
    )
    def test_malformed_overrides_are_rejected(self, raw):
        with pytest.raises(NetworkProviderConfigError, match="providers.json"):
            parse_provider_id_overrides(raw, "providers.json")


def _providers_file(monkeypatch, tmp_path, content):
    path = tmp_path / "network_group_providers.json"
    path.write_text(content)
    monkeypatch.setenv(PROVIDER_CONFIG_OVERRIDE_PATH_ENV, str(path))
    return path


class TestLoadProviderIdOverrides:
    @pytest.mark.unit
    def test_reads_the_configured_file(self, monkeypatch, tmp_path):
        _providers_file(monkeypatch, tmp_path, '{"ka": {"advisory": "advisory.ka.com"}}')

        assert load_provider_id_overrides() == {"ka": {"advisory": "advisory.ka.com"}}

    @pytest.mark.unit
    def test_unset_path_uses_the_bundled_empty_default(self, monkeypatch):
        monkeypatch.delenv(PROVIDER_CONFIG_OVERRIDE_PATH_ENV, raising=False)

        assert DEFAULT_PROVIDER_CONFIG_OVERRIDE_PATH.is_file()
        assert load_provider_id_overrides() == {}

    @pytest.mark.unit
    def test_missing_file_raises(self, monkeypatch, tmp_path):
        missing = tmp_path / "absent.json"
        monkeypatch.setenv(PROVIDER_CONFIG_OVERRIDE_PATH_ENV, str(missing))

        with pytest.raises(NetworkProviderConfigError, match="absent.json"):
            load_provider_id_overrides()

    @pytest.mark.unit
    def test_malformed_file_raises(self, monkeypatch, tmp_path):
        _providers_file(monkeypatch, tmp_path, "{broken")

        with pytest.raises(NetworkProviderConfigError, match="network_group_providers.json"):
            load_provider_id_overrides()

    @pytest.mark.unit
    def test_example_file_is_valid(self):
        example = Path(__file__).parents[1] / "deploy" / "network_group_providers.example.json"

        assert parse_provider_id_overrides(example.read_text(), str(example))["ka"] == {
            "advisory": "advisory.ka.com",
            "scheme": "scheme.ka.com",
        }


class TestLoadProviderNaming:
    @pytest.fixture(autouse=True)
    def _clear_naming_env(self, monkeypatch):
        for name in (
            "NETWORK_PROVIDER_ID_TEMPLATE",
            "NETWORK_GROUP_PROVIDER_NAME_TEMPLATE",
            "NETWORK_ADVISORY_PROVIDER_NAME",
            "NETWORK_SCHEMES_PROVIDER_NAME",
        ):
            monkeypatch.delenv(name, raising=False)

    @pytest.mark.unit
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv(PROVIDER_CONFIG_OVERRIDE_PATH_ENV, raising=False)

        naming = load_provider_naming()

        assert naming.shared_instance == "bv"
        assert naming.id_template == "{kind}.{instance}.provider.oan.dev"
        assert naming.group_name_template == "{group_name} {provider_name}"
        assert naming.provider_names == {
            "advisory": "Advisories from documents",
            "scheme": "Schemes from documents",
        }
        assert naming.provider_id_overrides == {}

    @pytest.mark.unit
    def test_carries_the_provider_id_overrides(self, monkeypatch, tmp_path):
        _providers_file(monkeypatch, tmp_path, '{"ka": {"scheme": "scheme.ka.com"}}')

        assert load_provider_naming().provider_id_overrides == {"ka": {"scheme": "scheme.ka.com"}}

    @pytest.mark.unit
    def test_reads_overrides(self, monkeypatch):
        monkeypatch.setenv("PORTAL_INSTANCE", "Portal")
        monkeypatch.setenv("NETWORK_PROVIDER_ID_TEMPLATE", "{instance}.{kind}.example.net")
        monkeypatch.setenv("NETWORK_GROUP_PROVIDER_NAME_TEMPLATE", "{provider_name} - {group_name}")
        monkeypatch.setenv("NETWORK_ADVISORY_PROVIDER_NAME", "Advice")
        monkeypatch.setenv("NETWORK_SCHEMES_PROVIDER_NAME", "Schemes")

        naming = load_provider_naming()

        assert naming.shared_instance == "portal"
        assert naming.id_template == "{instance}.{kind}.example.net"
        assert naming.group_name_template == "{provider_name} - {group_name}"
        assert naming.provider_names == {"advisory": "Advice", "scheme": "Schemes"}

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "env_name,template",
        [
            ("NETWORK_PROVIDER_ID_TEMPLATE", "{state}.provider.oan.dev"),
            ("NETWORK_PROVIDER_ID_TEMPLATE", "{0}.provider.oan.dev"),
            ("NETWORK_PROVIDER_ID_TEMPLATE", "{kind.provider.oan.dev"),
            ("NETWORK_PROVIDER_ID_TEMPLATE", "{kind.x}.{instance}.provider.oan.dev"),
            ("NETWORK_GROUP_PROVIDER_NAME_TEMPLATE", "{group} {provider_name}"),
        ],
    )
    def test_unknown_placeholders_are_rejected(self, monkeypatch, env_name, template):
        monkeypatch.setenv(env_name, template)

        with pytest.raises(NetworkProviderConfigError, match=env_name):
            load_provider_naming()

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "template",
        ["{kind}.oan.dev", "{instance}.provider.oan.dev", "provider.oan.dev"],
    )
    def test_id_template_must_name_both_kind_and_instance(self, monkeypatch, template):
        monkeypatch.setenv("NETWORK_PROVIDER_ID_TEMPLATE", template)

        with pytest.raises(NetworkProviderConfigError, match="must use"):
            load_provider_naming()


class TestBootRefusesMalformedProviderMap:
    @pytest.fixture
    def broken_map(self, monkeypatch, tmp_path):
        _providers_file(monkeypatch, tmp_path, "{broken")
        monkeypatch.setenv("TRANSLATION_PROVIDER", "none")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_worker_does_not_start(self, broken_map, monkeypatch):
        import pipeline.worker as worker

        async def never_connect(*args, **kwargs):
            raise AssertionError("worker connected to Temporal despite a bad provider map")

        monkeypatch.setattr(worker.Client, "connect", never_connect)

        with pytest.raises(NetworkProviderConfigError):
            await worker.main()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_api_does_not_start(self, broken_map, monkeypatch):
        import pipeline.api as api

        async def never_connect(*args, **kwargs):
            raise AssertionError("API connected to Temporal despite a bad provider map")

        monkeypatch.setattr(api.Client, "connect", never_connect)

        with pytest.raises(NetworkProviderConfigError):
            async with api.lifespan(api.app):
                pass
