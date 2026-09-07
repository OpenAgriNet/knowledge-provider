"""Unit tests for scripts/mock_discovery_service.py.

The mock is what the pipeline is pointed at during local end-to-end runs, so
its answer shape is a contract: `pipeline/discovery_publish_service.py` reads
`message.results[].status` out of it. Loaded via importlib because `scripts/`
is not a package.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

MOCK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mock_discovery_service.py"

ADVISORY_CATALOG_ID = "oan.knowledgeprovider.advisory"


def _load_mock():
    spec = importlib.util.spec_from_file_location("mock_discovery_service", MOCK_PATH)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the module uses `from __future__ import
    # annotations`, so pydantic resolves its models' forward refs through
    # sys.modules and fails with class-not-fully-defined without this.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mock_service(monkeypatch):
    monkeypatch.delenv("MOCK_DISCOVERY_SERVICE_RESULT_STATUS", raising=False)
    module = _load_mock()
    module.recorded_requests.clear()
    yield module
    sys.modules.pop("mock_discovery_service", None)


@pytest.fixture
def client(mock_service):
    return TestClient(mock_service.app)


def _context():
    return {
        "action": "catalog/publish",
        "version": "2.0.0",
        "senderId": "docs-pipeline-bv",
        "transactionId": "txn-1",
        "messageId": "msg-1",
        "networkId": "da.gov.in/vistaar",
    }


def _envelope(catalogs=None):
    return {
        "context": _context(),
        "message": {
            "catalogs": catalogs
            if catalogs is not None
            else [{"id": ADVISORY_CATALOG_ID, "resources": [{"id": "res-1"}]}]
        },
    }


class TestPublishResults:
    @pytest.mark.unit
    def test_accepts_a_well_formed_envelope(self, client):
        response = client.post("/publish", json=_envelope())

        assert response.status_code == 200
        results = response.json()["message"]["results"]
        assert results == [
            {
                "catalogId": ADVISORY_CATALOG_ID,
                "status": "ACCEPTED",
                "stats": {"itemCount": 1, "providerCount": 1, "categoryCount": 1},
                "errors": [],
            }
        ]

    @pytest.mark.unit
    def test_echoes_the_request_context_as_on_publish(self, client):
        context = client.post("/publish", json=_envelope()).json()["context"]

        assert context["action"] == "catalog/on_publish"
        assert context["transactionId"] == "txn-1"
        assert context["senderId"] == "docs-pipeline-bv"
        # A fresh messageId per response, per Beckn convention.
        assert context["messageId"] != "msg-1"

    @pytest.mark.unit
    def test_one_result_per_submitted_catalog(self, client):
        catalogs = [
            {"id": ADVISORY_CATALOG_ID, "resources": [{"id": "res-1"}]},
            {"id": "oan.knowledgeprovider.schemes", "resources": [{"id": "res-2"}]},
        ]

        results = client.post("/publish", json=_envelope(catalogs)).json()["message"]["results"]

        assert [r["catalogId"] for r in results] == [c["id"] for c in catalogs]

    @pytest.mark.unit
    def test_item_count_reflects_the_resources_sent(self, client):
        catalogs = [{"id": ADVISORY_CATALOG_ID, "resources": [{"id": "a"}, {"id": "b"}]}]

        results = client.post("/publish", json=_envelope(catalogs)).json()["message"]["results"]

        assert results[0]["stats"]["itemCount"] == 2

    @pytest.mark.unit
    def test_forced_rejection_exercises_the_pipeline_failure_path(self, monkeypatch, client):
        monkeypatch.setenv("MOCK_DISCOVERY_SERVICE_RESULT_STATUS", "rejected")

        results = client.post("/publish", json=_envelope()).json()["message"]["results"]

        # Still a 200: the whole point is that the status lives in the body.
        assert results[0]["status"] == "REJECTED"
        assert results[0]["errors"][0]["code"] == "SCH_SCHEMA_VALIDATION_FAILED"

    @pytest.mark.unit
    def test_repeated_transaction_id_with_a_new_message_id_is_fine(self, client):
        # Every activity retry reuses transactionId and regenerates messageId.
        first = _envelope()
        second = _envelope()
        second["context"]["messageId"] = "msg-2"

        assert client.post("/publish", json=first).status_code == 200
        assert client.post("/publish", json=second).status_code == 200


class TestEnvelopeValidation:
    @pytest.mark.unit
    def test_empty_catalogs_is_rejected(self, client):
        # message.catalogs is minItems: 1 - the stub this whole change replaced.
        response = client.post("/publish", json=_envelope([]))

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SCH_SCHEMA_VALIDATION_FAILED"

    @pytest.mark.unit
    def test_catalog_without_resources_is_rejected(self, client):
        response = client.post("/publish", json=_envelope([{"id": "x", "resources": []}]))

        assert response.status_code == 400

    @pytest.mark.unit
    @pytest.mark.parametrize("field", ["action", "senderId", "transactionId", "messageId"])
    def test_context_must_identify_the_sender(self, client, field):
        envelope = _envelope()
        envelope["context"].pop(field)

        response = client.post("/publish", json=envelope)

        assert response.status_code == 400

    @pytest.mark.unit
    def test_unknown_catalog_fields_are_carried_not_refused(self, client):
        # The real service treats catalogs as opaque JSON; so must the mock.
        catalogs = [
            {
                "id": ADVISORY_CATALOG_ID,
                "bppId": "docs-pipeline-bv",
                "bppUri": "https://docs.example.gov.in",
                "descriptor": {"name": "Agricultural advisory from documents"},
                "isActive": True,
                "resources": [{"id": "res-1", "resourceAttributes": {"@type": "anything"}}],
            }
        ]

        assert client.post("/publish", json=_envelope(catalogs)).status_code == 200


class TestRequestRecorder:
    @pytest.mark.unit
    def test_records_publishes_for_inspection(self, client, mock_service):
        client.post("/publish", json=_envelope())

        recorded = client.get("/_requests").json()
        assert len(recorded) == 1
        assert recorded[0]["path"] == "/publish"
        assert recorded[0]["body"]["message"]["catalogs"][0]["id"] == ADVISORY_CATALOG_ID
        assert "headers" in recorded[0]

    @pytest.mark.unit
    def test_records_rejected_envelopes_too(self, client):
        # An envelope the mock refuses is exactly the one worth inspecting.
        client.post("/publish", json=_envelope([]))

        assert len(client.get("/_requests").json()) == 1

    @pytest.mark.unit
    def test_does_not_record_the_recorder(self, client):
        client.get("/_requests")

        assert client.get("/_requests").json() == []
