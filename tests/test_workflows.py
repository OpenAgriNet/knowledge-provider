"""Tests for pipeline/workflows.py module-level configuration."""

import pytest

from pipeline.workflows import _network_publish_retry_policy


class TestNetworkPublishRetryPolicy:
    @pytest.mark.unit
    def test_defaults_when_unset(self, monkeypatch):
        for var in (
            "DISCOVERY_SERVICE_PUBLISH_INITIAL_INTERVAL_SECONDS",
            "DISCOVERY_SERVICE_PUBLISH_BACKOFF_COEFFICIENT",
            "DISCOVERY_SERVICE_PUBLISH_MAX_INTERVAL_SECONDS",
            "DISCOVERY_SERVICE_PUBLISH_MAX_ATTEMPTS",
        ):
            monkeypatch.delenv(var, raising=False)

        policy = _network_publish_retry_policy()

        assert policy.initial_interval.total_seconds() == 30
        assert policy.backoff_coefficient == 2.0
        assert policy.maximum_interval.total_seconds() == 300
        assert policy.maximum_attempts == 5

    @pytest.mark.unit
    def test_reads_overrides_from_env(self, monkeypatch):
        monkeypatch.setenv("DISCOVERY_SERVICE_PUBLISH_INITIAL_INTERVAL_SECONDS", "10")
        monkeypatch.setenv("DISCOVERY_SERVICE_PUBLISH_BACKOFF_COEFFICIENT", "1.5")
        monkeypatch.setenv("DISCOVERY_SERVICE_PUBLISH_MAX_INTERVAL_SECONDS", "120")
        monkeypatch.setenv("DISCOVERY_SERVICE_PUBLISH_MAX_ATTEMPTS", "3")

        policy = _network_publish_retry_policy()

        assert policy.initial_interval.total_seconds() == 10
        assert policy.backoff_coefficient == 1.5
        assert policy.maximum_interval.total_seconds() == 120
        assert policy.maximum_attempts == 3
