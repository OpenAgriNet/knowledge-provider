import pytest

from pipeline.network_group import (
    GROUP_PROVIDER_IDS_ENV,
    GroupProviderConfigError,
    group_for_instance,
    load_group_provider_ids,
    parse_group_provider_ids,
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


class TestParseGroupProviderIds:
    @pytest.mark.unit
    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_unset_means_no_group_has_a_provider(self, raw):
        assert parse_group_provider_ids(raw) == {}

    @pytest.mark.unit
    def test_keys_are_case_insensitive_and_values_trimmed(self):
        parsed = parse_group_provider_ids('{"MH": " mh.provider.oan.dev ", "ka": "ka.provider.oan.dev"}')

        assert parsed == {"mh": "mh.provider.oan.dev", "ka": "ka.provider.oan.dev"}

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            '["mh.provider.oan.dev"]',
            '"mh.provider.oan.dev"',
            '{"mh": 42}',
            '{"mh": ""}',
            '{"": "mh.provider.oan.dev"}',
            '{"MH": "a", "mh": "b"}',
        ],
    )
    def test_malformed_map_is_rejected(self, raw):
        with pytest.raises(GroupProviderConfigError, match=GROUP_PROVIDER_IDS_ENV):
            parse_group_provider_ids(raw)


class TestLoadGroupProviderIds:
    @pytest.mark.unit
    def test_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv(GROUP_PROVIDER_IDS_ENV, '{"mh": "mh.provider.oan.dev"}')

        assert load_group_provider_ids() == {"mh": "mh.provider.oan.dev"}

    @pytest.mark.unit
    def test_malformed_environment_raises(self, monkeypatch):
        monkeypatch.setenv(GROUP_PROVIDER_IDS_ENV, "{broken")

        with pytest.raises(GroupProviderConfigError):
            load_group_provider_ids()


class TestBootRefusesMalformedProviderMap:
    @pytest.fixture
    def broken_map(self, monkeypatch):
        monkeypatch.setenv(GROUP_PROVIDER_IDS_ENV, "{broken")
        monkeypatch.setenv("TRANSLATION_PROVIDER", "none")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_worker_does_not_start(self, broken_map, monkeypatch):
        import pipeline.worker as worker

        async def never_connect(*args, **kwargs):
            raise AssertionError("worker connected to Temporal despite a bad provider map")

        monkeypatch.setattr(worker.Client, "connect", never_connect)

        with pytest.raises(GroupProviderConfigError):
            await worker.main()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_api_does_not_start(self, broken_map, monkeypatch):
        import pipeline.api as api

        async def never_connect(*args, **kwargs):
            raise AssertionError("API connected to Temporal despite a bad provider map")

        monkeypatch.setattr(api.Client, "connect", never_connect)

        with pytest.raises(GroupProviderConfigError):
            async with api.lifespan(api.app):
                pass
