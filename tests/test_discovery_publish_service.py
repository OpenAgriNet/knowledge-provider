"""Unit tests for DiscoveryPublishService."""

import logging
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

        service = DiscoveryPublishService(
            endpoint="https://explicit.example.com",
            sender_id="explicit-sender",
        )

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
            result = service.publish(transaction_id="txn-123", document_kind="advisory")

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args.args[0] == "https://discovery.example.com/publish"

        envelope = call_args.kwargs["json"]
        assert envelope["context"]["action"] == "publish"
        assert envelope["context"]["transactionId"] == "txn-123"
        assert envelope["context"]["senderId"] == "docs-pipeline-bv"
        assert envelope["context"]["messageId"]  # generated, non-empty
        assert envelope["context"]["timestamp"].endswith("Z")
        assert envelope["context"]["version"] == "2.0.0"
        # Sender id doubles as networkId.
        assert envelope["context"]["networkId"] == "docs-pipeline-bv"
        assert "receiverId" not in envelope["context"]

        catalogs = envelope["message"]["catalogs"]
        assert len(catalogs) == 1
        assert catalogs[0]["id"] == "cat-oan-knowledge-provider-advisories"
        assert "bppId" not in catalogs[0]
        assert "bppUri" not in catalogs[0]
        assert envelope["message"]["publishDirectives"] == [
            {
                "catalogId": "cat-oan-knowledge-provider-advisories",
                "catalogType": "REGULAR",
                "updateMode": "MERGE",
            }
        ]

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
            first = service.publish(transaction_id="txn-shared", document_kind="advisory")
            second = service.publish(transaction_id="txn-shared", document_kind="advisory")

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
                service.publish(transaction_id="txn-123", document_kind="advisory")

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
                service.publish(transaction_id="txn-123", document_kind="advisory")


class TestDiscoveryPublishServiceSchemeCatalog:
    @pytest.mark.unit
    def test_scheme_kind_publishes_the_schemes_catalog(self, monkeypatch):
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
            result = service.publish(transaction_id="txn-9", document_kind="scheme")

        envelope = mock_client.post.call_args.kwargs["json"]
        catalog = envelope["message"]["catalogs"][0]
        assert catalog["id"] == "cat-oan-knowledge-provider-schemes"
        assert envelope["message"]["publishDirectives"][0]["catalogId"] == (
            "cat-oan-knowledge-provider-schemes"
        )
        assert result["skipped"] is False


class TestDiscoveryPublishServiceSkipsUnmappedKinds:
    @pytest.mark.unit
    @pytest.mark.parametrize("kind", ["document", "video", "how_to_faq", "", None])
    def test_no_http_call_is_made(self, monkeypatch, kind):
        from pipeline.discovery_publish_service import DiscoveryPublishService

        monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "https://discovery.example.com")
        monkeypatch.setenv("NETWORK_SENDER_ID", "docs-pipeline-bv")
        service = DiscoveryPublishService()

        mock_client = MagicMock()

        with patch("pipeline.discovery_publish_service.httpx.Client", return_value=mock_client):
            result = service.publish(transaction_id="txn-skip", document_kind=kind)

        mock_client.post.assert_not_called()
        assert result["skipped"] is True
        assert result["envelope"] is None
        assert result["status_code"] is None


LOGGER_NAME = "pipeline.discovery_publish_service"


def _logging_service(monkeypatch):
    from pipeline.discovery_publish_service import DiscoveryPublishService

    monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "https://discovery.example.com")
    monkeypatch.setenv("NETWORK_SENDER_ID", "docs-pipeline-bv")
    return DiscoveryPublishService()


def _ok_client():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.status_code = 200
    response.text = "{}"

    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.post.return_value = response
    return client


class TestDiscoveryPublishServiceLogging:
    """Every line carries workflow_id - the correlation key for worker code."""

    @pytest.mark.unit
    def test_logs_the_request_and_the_response_status(self, monkeypatch, caplog):
        service = _logging_service(monkeypatch)

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            with patch(
                "pipeline.discovery_publish_service.httpx.Client", return_value=_ok_client()
            ):
                service.publish(
                    transaction_id="txn-log", document_kind="advisory", workflow_id="wf-log"
                )

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "network_publish_url=https://discovery.example.com/publish" in m
            and "workflow_id=wf-log" in m
            and "transaction_id=txn-log" in m
            and "catalog_id=cat-oan-knowledge-provider-advisories" in m
            for m in messages
        )
        assert any("network_publish_status=200" in m for m in messages)

    @pytest.mark.unit
    def test_logs_an_error_when_the_call_fails(self, monkeypatch, caplog):
        # The activity records no artifact on failure, so the log is the only trace.
        service = _logging_service(monkeypatch)
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.side_effect = httpx.ConnectError("connection refused")

        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            with patch("pipeline.discovery_publish_service.httpx.Client", return_value=client):
                with pytest.raises(httpx.ConnectError):
                    service.publish(
                        transaction_id="txn-log", document_kind="advisory", workflow_id="wf-log"
                    )

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            "network_publish_failed=True" in m and "workflow_id=wf-log" in m for m in errors
        )

    @pytest.mark.unit
    def test_skipped_publish_logs_no_request(self, monkeypatch, caplog):
        service = _logging_service(monkeypatch)

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            with patch(
                "pipeline.discovery_publish_service.httpx.Client", return_value=MagicMock()
            ):
                service.publish(
                    transaction_id="txn-log", document_kind="video", workflow_id="wf-log"
                )

        messages = [r.getMessage() for r in caplog.records]
        assert any("network_publish_skipped=True" in m for m in messages)
        assert not any("network_publish_url=" in m for m in messages)
