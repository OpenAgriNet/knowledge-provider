"""Unit tests for DiscoveryPublishService."""

import json
import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest


def _on_publish_response(catalog_id, status, errors=None, status_code=200):
    """A mock httpx.Response carrying one CatalogProcessingResult."""
    body = {
        "context": {"action": "catalog/on_publish"},
        "message": {
            "results": [
                {
                    "catalogId": catalog_id,
                    "status": status,
                    "errors": errors or [],
                }
            ]
        },
    }
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.status_code = status_code
    response.text = json.dumps(body)
    response.json = MagicMock(return_value=body)
    return response


def _client_returning(response):
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.post.return_value = response
    return client


def _service(monkeypatch):
    from pipeline.discovery_publish_service import DiscoveryPublishService

    monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "https://discovery.example.com")
    monkeypatch.setenv("NETWORK_SENDER_ID", "docs-pipeline-bv")
    return DiscoveryPublishService()



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
    def test_explicit_endpoint_overrides_env(self, monkeypatch):
        from pipeline.discovery_publish_service import DiscoveryPublishService

        monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "https://env.example.com")
        monkeypatch.setenv("NETWORK_SENDER_ID", " provider.oan.dev ")

        service = DiscoveryPublishService(endpoint="https://explicit.example.com")

        assert service.endpoint == "https://explicit.example.com"
        assert service.sender_id == "provider.oan.dev"


class TestDiscoveryPublishServicePublish:
    @pytest.mark.unit
    def test_publish_success_envelope_shape(self, monkeypatch):
        from pipeline.discovery_publish_service import DiscoveryPublishService

        monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "https://discovery.example.com")
        monkeypatch.setenv("NETWORK_SENDER_ID", "docs-pipeline-bv")
        service = DiscoveryPublishService()

        mock_response = _on_publish_response("cat-oan-knowledge-provider-advisories", "ACCEPTED")

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
        assert envelope["context"]["action"] == "catalog/publish"
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
        assert json.loads(result["response_body"])["message"]["results"][0]["status"] == "ACCEPTED"
        assert result["result_status"] == "ACCEPTED"
        assert result["errors"] == []

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


class TestDiscoveryPublishServiceResult:
    """A 200 is only an ACK - acceptance lives in message.results[].status."""

    @pytest.mark.unit
    @pytest.mark.parametrize("status", ["ACCEPTED", "PARTIAL", "REJECTED"])
    def test_reports_the_result_status_for_our_catalog(self, monkeypatch, status):
        service = _service(monkeypatch)
        response = _on_publish_response("cat-oan-knowledge-provider-advisories", status)

        with patch(
            "pipeline.discovery_publish_service.httpx.Client",
            return_value=_client_returning(response),
        ):
            result = service.publish(transaction_id="txn-v", document_kind="advisory")

        assert result["result_status"] == status

    @pytest.mark.unit
    def test_carries_the_errors_array_through(self, monkeypatch):
        service = _service(monkeypatch)
        errors = [{"code": "SCH_SCHEMA_VALIDATION_FAILED", "message": "topics is required"}]
        response = _on_publish_response(
            "cat-oan-knowledge-provider-advisories", "REJECTED", errors=errors
        )

        with patch(
            "pipeline.discovery_publish_service.httpx.Client",
            return_value=_client_returning(response),
        ):
            result = service.publish(transaction_id="txn-v", document_kind="advisory")

        assert result["errors"] == errors

    @pytest.mark.unit
    def test_picks_our_catalog_out_of_several_results(self, monkeypatch):
        service = _service(monkeypatch)
        body = {
            "message": {
                "results": [
                    {"catalogId": "someone.else", "status": "REJECTED", "errors": []},
                    {"catalogId": "cat-oan-knowledge-provider-advisories", "status": "ACCEPTED"},
                ]
            }
        }
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.status_code = 200
        response.text = json.dumps(body)
        response.json = MagicMock(return_value=body)

        with patch(
            "pipeline.discovery_publish_service.httpx.Client",
            return_value=_client_returning(response),
        ):
            result = service.publish(transaction_id="txn-v", document_kind="advisory")

        assert result["result_status"] == "ACCEPTED"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "body",
        [
            {"message": {"ack": {"status": "ACK"}}},  # a bare v1-style ACK
            {"message": {"results": []}},
            {},
        ],
    )
    def test_unreadable_result_is_unknown_not_a_failure(self, monkeypatch, body):
        # A peer that deviates from the spec must not be able to stall the
        # pipeline, so an unparseable body reports status None rather than raising.
        service = _service(monkeypatch)
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.status_code = 200
        response.text = json.dumps(body)
        response.json = MagicMock(return_value=body)

        with patch(
            "pipeline.discovery_publish_service.httpx.Client",
            return_value=_client_returning(response),
        ):
            result = service.publish(transaction_id="txn-v", document_kind="advisory")

        assert result["result_status"] is None
        assert result["errors"] == []

    @pytest.mark.unit
    def test_non_json_body_is_unknown_not_a_failure(self, monkeypatch):
        service = _service(monkeypatch)
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.status_code = 200
        response.text = "<html>502 Bad Gateway</html>"
        response.json = MagicMock(side_effect=ValueError("not json"))

        with patch(
            "pipeline.discovery_publish_service.httpx.Client",
            return_value=_client_returning(response),
        ):
            result = service.publish(transaction_id="txn-v", document_kind="advisory")

        assert result["result_status"] is None
        assert result["errors"] == []

    @pytest.mark.unit
    def test_skipped_publish_has_no_result_status(self, monkeypatch):
        service = _service(monkeypatch)

        with patch("pipeline.discovery_publish_service.httpx.Client", return_value=MagicMock()):
            result = service.publish(transaction_id="txn-v", document_kind="video")

        assert result["skipped"] is True
        assert result["result_status"] is None
        assert result["errors"] == []


LOGGER_NAME = "pipeline.discovery_publish_service"


class TestDiscoveryPublishServiceLogging:
    """Every line carries workflow_id - the correlation key for worker code."""

    @pytest.mark.unit
    def test_logs_the_request_and_the_response_status(self, monkeypatch, caplog):
        service = _service(monkeypatch)

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            ok = _client_returning(
                _on_publish_response("cat-oan-knowledge-provider-advisories", "ACCEPTED")
            )
            with patch("pipeline.discovery_publish_service.httpx.Client", return_value=ok):
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
        service = _service(monkeypatch)
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
            "network_publish_failed=True" in m
            and "workflow_id=wf-log" in m
            and "transaction_id=txn-log" in m
            for m in errors
        )

    @pytest.mark.unit
    def test_skipped_publish_logs_no_request(self, monkeypatch, caplog):
        service = _service(monkeypatch)

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


def _use_group_providers(monkeypatch, tmp_path, content):
    path = tmp_path / "network_group_providers.json"
    path.write_text(content)
    monkeypatch.setenv("PROVIDER_CONFIG_OVERRIDE_PATH", str(path))


@pytest.fixture(autouse=True)
def _default_provider_naming(monkeypatch):
    for name in (
        "PORTAL_INSTANCE",
        "NETWORK_PROVIDER_ID_TEMPLATE",
        "NETWORK_GROUP_CATALOG_NAME_TEMPLATE",
        "NETWORK_ADVISORY_CATALOG_NAME",
        "NETWORK_SCHEMES_CATALOG_NAME",
        "PROVIDER_CONFIG_OVERRIDE_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def _publish(monkeypatch, kind, group, sender_id="provider.oan.dev"):
    from pipeline.discovery_publish_service import DiscoveryPublishService

    monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "https://discovery.example.com")
    monkeypatch.setenv("NETWORK_SENDER_ID", sender_id)
    service = DiscoveryPublishService()
    client = _client_returning(_on_publish_response("any", "ACCEPTED"))
    with patch("pipeline.discovery_publish_service.httpx.Client", return_value=client):
        result = service.publish(transaction_id="txn", document_kind=kind, group=group)
    return client.post.call_args.kwargs["json"], result


class TestDiscoveryPublishServiceGroupProvider:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "kind,catalog_id,resource_id,catalog_name",
        [
            ("advisory", "mh-cat-oan-knowledge-provider-advisories", "mh-res-oan-knowledge-provider-advisories",
             "Maharashtra Advisories from documents"),
            ("scheme", "mh-cat-oan-knowledge-provider-schemes", "mh-res-oan-knowledge-provider-schemes",
             "Maharashtra Schemes from documents"),
        ],
    )
    def test_group_publishes_both_kinds_under_one_provider(self, monkeypatch, kind, catalog_id, resource_id, catalog_name):
        envelope, result = _publish(monkeypatch, kind, "mh")

        catalog = envelope["message"]["catalogs"][0]
        assert catalog["id"] == catalog_id
        assert catalog["resources"][0]["id"] == resource_id
        assert catalog["descriptor"] == {"name": catalog_name}
        assert catalog["provider"] == {
            "id": "mh.provider.oan.dev",
            "descriptor": {"code": "mh.provider.oan.dev", "name": "Maharashtra"},
        }
        assert envelope["message"]["publishDirectives"][0]["catalogId"] == catalog_id
        assert envelope["context"]["networkId"] == "provider.oan.dev"
        assert result["skipped"] is False

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "kind,catalog_id,catalog_name",
        [
            ("advisory", "cat-oan-knowledge-provider-advisories", "Advisories from documents"),
            ("scheme", "cat-oan-knowledge-provider-schemes", "Schemes from documents"),
        ],
    )
    def test_no_group_publishes_under_the_shared_instance(self, monkeypatch, kind, catalog_id, catalog_name):
        envelope, _ = _publish(monkeypatch, kind, None)

        catalog = envelope["message"]["catalogs"][0]
        assert catalog["id"] == catalog_id
        assert catalog["descriptor"] == {"name": catalog_name}
        assert catalog["provider"] == {
            "id": "bv.provider.oan.dev",
            "descriptor": {"code": "bv.provider.oan.dev", "name": "Bharat Vistaar"},
        }

    @pytest.mark.unit
    def test_provider_id_follows_the_sender_id(self, monkeypatch):
        envelope, _ = _publish(monkeypatch, "scheme", "mh", sender_id="provider.oan.prod")

        assert envelope["message"]["catalogs"][0]["provider"]["id"] == "mh.provider.oan.prod"

    @pytest.mark.unit
    def test_overridden_group_publishes_under_its_configured_provider(self, monkeypatch, tmp_path):
        _use_group_providers(monkeypatch, tmp_path, '{"KA": "ka.example.com"}')

        envelope, _ = _publish(monkeypatch, "scheme", "ka")

        catalog = envelope["message"]["catalogs"][0]
        assert catalog["id"] == "ka-cat-oan-knowledge-provider-schemes"
        assert catalog["provider"] == {
            "id": "ka.example.com",
            "descriptor": {"code": "ka.example.com", "name": "Karnataka"},
        }

    @pytest.mark.unit
    def test_catalog_names_are_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("NETWORK_GROUP_CATALOG_NAME_TEMPLATE", "{group_name} - {catalog_name}")
        monkeypatch.setenv("NETWORK_SCHEMES_CATALOG_NAME", "Government schemes")

        envelope, _ = _publish(monkeypatch, "scheme", "mh")

        assert envelope["message"]["catalogs"][0]["descriptor"] == {"name": "Maharashtra - Government schemes"}

    @pytest.mark.unit
    def test_unmapped_kind_is_skipped_for_a_group(self, monkeypatch):
        from pipeline.discovery_publish_service import DiscoveryPublishService

        monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "https://discovery.example.com")
        monkeypatch.setenv("NETWORK_SENDER_ID", "provider.oan.dev")
        service = DiscoveryPublishService()

        result = service.publish(transaction_id="txn-doc", document_kind="document", group="ka")

        assert result["skipped"] is True
        assert result["envelope"] is None

    @pytest.mark.unit
    def test_malformed_provider_map_fails_construction(self, monkeypatch, tmp_path):
        from pipeline.discovery_publish_service import DiscoveryPublishService
        from pipeline.network_group import NetworkProviderConfigError

        monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "https://discovery.example.com")
        monkeypatch.setenv("NETWORK_SENDER_ID", "provider.oan.dev")
        _use_group_providers(monkeypatch, tmp_path, "{broken")

        with pytest.raises(NetworkProviderConfigError):
            DiscoveryPublishService()

    @pytest.mark.unit
    def test_bad_provider_id_template_fails_construction(self, monkeypatch):
        from pipeline.discovery_publish_service import DiscoveryPublishService
        from pipeline.network_group import NetworkProviderConfigError

        monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "https://discovery.example.com")
        monkeypatch.setenv("NETWORK_SENDER_ID", "provider.oan.dev")
        monkeypatch.setenv("NETWORK_PROVIDER_ID_TEMPLATE", "{state}.{network_id}")

        with pytest.raises(NetworkProviderConfigError, match="NETWORK_PROVIDER_ID_TEMPLATE"):
            DiscoveryPublishService()
