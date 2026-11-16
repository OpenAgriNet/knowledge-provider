"""Tests for instance (tenant) code → state name resolution."""

import pytest


class TestGroupDisplayName:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "code,expected",
        [
            ("bv", "Bharat Vistaar"),
            ("BV", "Bharat Vistaar"),
            ("mh", "Maharashtra"),
            ("ka", "Karnataka"),
            ("up", "Uttar Pradesh"),
            ("gj", "Gujarat"),
            ("tn", "Tamil Nadu"),
            ("wb", "West Bengal"),
            ("dl", "Delhi"),
            ("jk", "Jammu and Kashmir"),
            ("default", "Default"),
        ],
    )
    def test_known_codes(self, code, expected):
        from pipeline.network.release_policy import instance_display_name

        assert instance_display_name(code) == expected

    @pytest.mark.unit
    @pytest.mark.parametrize("code,expected", [("or", "Odisha"), ("od", "Odisha"), ("ts", "Telangana"), ("tg", "Telangana")])
    def test_legacy_code_aliases(self, code, expected):
        from pipeline.network.release_policy import instance_display_name

        assert instance_display_name(code) == expected

    @pytest.mark.unit
    def test_blank_falls_back_to_default(self):
        from pipeline.network.release_policy import instance_display_name

        assert instance_display_name("") == "Default"
        assert instance_display_name(None) == "Default"

    @pytest.mark.unit
    def test_unknown_code_is_made_presentable(self):
        """A new state must degrade to a readable label, never break ingestion."""
        from pipeline.network.release_policy import instance_display_name

        assert instance_display_name("new-state") == "New State"


class TestProdStageDisabled:
    """DISABLE_PROD_SETTING — turns the PROD half of the pipeline off."""

    @pytest.mark.unit
    @pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", "YES"])
    def test_truthy_values_disable_prod(self, monkeypatch, value):
        from pipeline.network.release_policy import prod_stage_disabled

        monkeypatch.setenv("DISABLE_PROD_SETTING", value)
        assert prod_stage_disabled() is True

    @pytest.mark.unit
    @pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "", "anything-else"])
    def test_other_values_keep_prod_enabled(self, monkeypatch, value):
        from pipeline.network.release_policy import prod_stage_disabled

        monkeypatch.setenv("DISABLE_PROD_SETTING", value)
        assert prod_stage_disabled() is False

    @pytest.mark.unit
    def test_defaults_to_enabled_when_unset(self, monkeypatch):
        """Absent flag must keep the full pipeline — never silently skip PROD."""
        from pipeline.network.release_policy import prod_stage_disabled

        monkeypatch.delenv("DISABLE_PROD_SETTING", raising=False)
        assert prod_stage_disabled() is False

    @pytest.mark.unit
    def test_whitespace_is_tolerated(self, monkeypatch):
        from pipeline.network.release_policy import prod_stage_disabled

        monkeypatch.setenv("DISABLE_PROD_SETTING", "  true  ")
        assert prod_stage_disabled() is True


class TestProdOnlyStages:
    @pytest.mark.unit
    def test_prod_only_stages_are_the_three_promotion_stages(self):
        from pipeline.documents.models import PIPELINE_STAGES, PROD_ONLY_STAGES

        assert PROD_ONLY_STAGES == {"approval_for_prod", "ingesting_prod", "publishing_to_network"}
        # All three must still exist in the canonical order, or filtering is a no-op.
        stage_ids = {s[0] for s in PIPELINE_STAGES}
        assert PROD_ONLY_STAGES <= stage_ids

    @pytest.mark.unit
    def test_filtering_leaves_a_pipeline_ending_at_completed(self):
        from pipeline.documents.models import PIPELINE_STAGES, PROD_ONLY_STAGES

        remaining = [s[0] for s in PIPELINE_STAGES if s[0] not in PROD_ONLY_STAGES]

        assert remaining[-1] == "completed"
        assert remaining[-2] == "ingesting"  # DEV ingest flows straight to completed when PROD is disabled

    @pytest.mark.unit
    def test_publishing_to_network_is_prod_only(self):
        """It must never run without a successful PROD promotion (see ADR 0004)."""
        from pipeline.documents.models import PIPELINE_STAGES, PROD_ONLY_STAGES

        assert "publishing_to_network" in PROD_ONLY_STAGES

        stage_ids = [s[0] for s in PIPELINE_STAGES]
        assert stage_ids.index("publishing_to_network") == stage_ids.index("ingesting_prod") + 1
        assert stage_ids.index("publishing_to_network") == stage_ids.index("completed") - 1


def test_managed_networks_keep_their_collections_separate(monkeypatch):
    from pipeline.network.release_policy import managed_release_collections, managed_release_targets

    monkeypatch.setenv("ENABLE_PUBLISH_TO_NETWORK", "true")
    monkeypatch.setenv("NETWORK_CONNECTIONS", "dev,prod,peer")
    monkeypatch.setenv("NETWORK_CONNECTION_DEFAULT", "dev")
    monkeypatch.delenv("AUTO_RELEASE_NETWORK_ID", raising=False)
    for name in ("dev", "prod", "peer"):
        prefix = f"NETWORK_CONNECTION_{name.upper()}_"
        monkeypatch.setenv(prefix + "TYPE", "beckn")
        monkeypatch.setenv(prefix + "ID", f"test.{name}")
        monkeypatch.setenv(prefix + "SENDER_ID", f"provider.test.{name}")
        monkeypatch.setenv(prefix + "DISCOVERY_URL", f"https://{name}.example.test")
    monkeypatch.setenv("NETWORK_CONNECTION_DEV_RELEASE_COLLECTION", "dev-published")
    monkeypatch.setenv("NETWORK_CONNECTION_PROD_RELEASE_COLLECTION", "prod-published")

    assert managed_release_targets("advisory") == {"test.dev": "dev-published", "test.prod": "prod-published"}
    assert managed_release_collections() == {"dev-published": "test.dev", "prod-published": "test.prod"}

    monkeypatch.setenv("NETWORK_CONNECTION_PROD_SCHEME_RELEASE_COLLECTION", "dev-published")
    with pytest.raises(ValueError, match="separate release collection"):
        managed_release_targets("scheme")


def test_automatic_release_uses_collections_from_the_named_network(monkeypatch):
    from pipeline.network.release_policy import managed_release_collections, upload_release_target

    for key, value in {
        "ENABLE_PUBLISH_TO_NETWORK": "true",
        "NETWORK_CONNECTIONS": "dev",
        "NETWORK_CONNECTION_DEFAULT": "dev",
        "NETWORK_CONNECTION_DEV_TYPE": "beckn",
        "NETWORK_CONNECTION_DEV_ID": "test.dev",
        "NETWORK_CONNECTION_DEV_SENDER_ID": "provider.test.dev",
        "NETWORK_CONNECTION_DEV_DISCOVERY_URL": "https://dev.example.test",
        "NETWORK_CONNECTION_DEV_RELEASE_COLLECTION": "dev-advisories",
        "NETWORK_CONNECTION_DEV_SCHEME_RELEASE_COLLECTION": "dev-schemes",
        "AUTO_RELEASE_NETWORK_ID": "test.dev",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AUTO_RELEASE_COLLECTION", raising=False)
    monkeypatch.delenv("AUTO_RELEASE_SCHEME_COLLECTION", raising=False)

    assert upload_release_target("advisory") == ("test.dev", "dev-advisories")
    assert upload_release_target("scheme") == ("test.dev", "dev-schemes")
    assert managed_release_collections() == {"dev-advisories": "test.dev", "dev-schemes": "test.dev"}

    monkeypatch.setenv("AUTO_RELEASE_SCHEME_COLLECTION", "mismatched")
    with pytest.raises(ValueError, match="automatic and managed release collections must agree"):
        managed_release_collections()
