"""Unit tests for DiscoveryPublishService."""

from unittest.mock import MagicMock, patch

import httpx
import pytest


class TestDiscoveryPublishServiceConfig:
    @pytest.mark.unit
    def test_requires_endpoint_and_sender_id(self, monkeypatch):
        from pipeline.discovery_publish_service import DiscoveryPublishService

        monkeypatch.delenv("DISCOVERY_SERVICE_ENDPOINT", raising=False)
        monkeypatch.delenv("NETWORK_SENDER_ID", raising=False)

        with pytest.raises(RuntimeError, match="DISCOVERY_SERVICE_ENDPOINT"):
            DiscoveryPublishService()

    @pytest.mark.unit
    def test_missing_sender_id_alone_raises(self, monkeypatch):
        from pipeline.discovery_publish_service import DiscoveryPublishService

        monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "https://discovery.example.com")
        monkeypatch.delenv("NETWORK_SENDER_ID", raising=False)

        with pytest.raises(RuntimeError, match="NETWORK_SENDER_ID"):
            DiscoveryPublishService()

    @pytest.mark.unit
    def test_explicit_args_override_env(self, monkeypatch):
        from pipeline.discovery_publish_service import DiscoveryPublishService

        monkeypatch.delenv("DISCOVERY_SERVICE_ENDPOINT", raising=False)
        monkeypatch.delenv("NETWORK_SENDER_ID", raising=False)

        service = DiscoveryPublishService(endpoint="https://explicit.example.com", sender_id="explicit-sender")

        assert service.endpoint == "https://explicit.example.com"
        assert service.sender_id == "explicit-sender"


class TestDiscoveryPublishServicePublish:
    @pytest.mark.unit
    def test_publish_success_envelope_shape(self, monkeypatch):
        from pipeline.discovery_publish_service import DiscoveryPublishService

        monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "https://discovery.example.com")
        monkeypatch.setenv("NETWORK_SENDER_ID", "docs-pipeline-bv")
        service = DiscoveryPublishService()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"ack": true}'

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        with patch("pipeline.discovery_publish_service.httpx.Client", return_value=mock_client):
            result = service.publish(transaction_id="txn-123")

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args.args[0] == "https://discovery.example.com/publish"

        envelope = call_args.kwargs["json"]
        assert envelope["context"]["action"] == "catalog/publish"
        assert envelope["context"]["transactionId"] == "txn-123"
        assert envelope["context"]["senderId"] == "docs-pipeline-bv"
        assert envelope["context"]["messageId"]  # generated, non-empty
        assert envelope["context"]["timestamp"].endswith("Z")
        assert envelope["message"] == {"catalogs": []}

        assert result["envelope"] == envelope
        assert result["status_code"] == 200
        assert result["response_body"] == '{"ack": true}'

    @pytest.mark.unit
    def test_publish_generates_fresh_message_id_per_call(self, monkeypatch):
        from pipeline.discovery_publish_service import DiscoveryPublishService

        monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "https://discovery.example.com")
        monkeypatch.setenv("NETWORK_SENDER_ID", "docs-pipeline-bv")
        service = DiscoveryPublishService()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "{}"

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        with patch("pipeline.discovery_publish_service.httpx.Client", return_value=mock_client):
            first = service.publish(transaction_id="txn-shared")
            second = service.publish(transaction_id="txn-shared")

        assert first["envelope"]["context"]["transactionId"] == "txn-shared"
        assert second["envelope"]["context"]["transactionId"] == "txn-shared"
        assert first["envelope"]["context"]["messageId"] != second["envelope"]["context"]["messageId"]

    @pytest.mark.unit
    def test_publish_raises_on_http_error(self, monkeypatch):
        from pipeline.discovery_publish_service import DiscoveryPublishService

        monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "https://discovery.example.com")
        monkeypatch.setenv("NETWORK_SENDER_ID", "docs-pipeline-bv")
        service = DiscoveryPublishService()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("bad", request=MagicMock(), response=MagicMock())
        )

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        with patch("pipeline.discovery_publish_service.httpx.Client", return_value=mock_client):
            with pytest.raises(httpx.HTTPStatusError):
                service.publish(transaction_id="txn-123")

    @pytest.mark.unit
    def test_publish_raises_on_connection_error(self, monkeypatch):
        from pipeline.discovery_publish_service import DiscoveryPublishService

        monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "https://discovery.example.com")
        monkeypatch.setenv("NETWORK_SENDER_ID", "docs-pipeline-bv")
        service = DiscoveryPublishService()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = httpx.ConnectError("unreachable")

        with patch("pipeline.discovery_publish_service.httpx.Client", return_value=mock_client):
            with pytest.raises(httpx.ConnectError):
                service.publish(transaction_id="txn-123")
