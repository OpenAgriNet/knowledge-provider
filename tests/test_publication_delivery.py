from unittest.mock import Mock
from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError

from pipeline.network import publication_delivery
from pipeline.temporal.activities import publication as activities
from pipeline.network.adapters.beckn import BecknNetworkPublisher
from pipeline.network.network_publisher import (
    PublicationReceipt,
    PublishConfigError,
    get_network_adapter,
)


@pytest.mark.db
@pytest.mark.asyncio
async def test_recorded_acceptance_survives_artifact_failure_and_retries(
    db_connection, sample_document, monkeypatch,
):
    db = db_connection
    workflow_id = sample_document["workflow_id"]
    network_id = "test.local"
    transaction_id = str(uuid4())
    publisher = Mock()
    publisher.publish.return_value = PublicationReceipt(
        exchange={"result_status": "ACCEPTED"}, message_id="accepted-message",
        result_status="ACCEPTED", context_matches=True,
    )
    monkeypatch.setenv("ENABLE_PUBLISH_TO_NETWORK", "true")
    monkeypatch.setenv("DISCOVERY_NETWORK_ID", network_id)
    monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "http://discovery.test")
    monkeypatch.setenv("NETWORK_SENDER_ID", "provider.test.local")
    monkeypatch.setattr(BecknNetworkPublisher, "publish", publisher.publish)
    upload = Mock(side_effect=[
        OSError("artifact unavailable"),
        ("blob://docs/exchange.json", 10, "application/json"),
        ("blob://docs/exchange.json", 10, "application/json"),
    ])
    monkeypatch.setattr(activities, "_upload_artifact", upload)
    args = {
        "require_matched_receipt": True, "publication_network_id": network_id,
    }

    with pytest.raises(OSError, match="artifact unavailable"):
        await activities.publish_catalog_to_network(workflow_id, transaction_id, **args)
    with monkeypatch.context() as changed:
        changed.setenv("DISCOVERY_VISIBLE_TO", "different.test")
        with pytest.raises(ApplicationError, match="publication policy changed"):
            await activities.publish_catalog_to_network(workflow_id, transaction_id, **args)
    publisher.publish.assert_called_once()
    result = await activities.publish_catalog_to_network(workflow_id, transaction_id, **args)
    assert result["message_id"] == "accepted-message"
    await activities.publish_catalog_to_network(workflow_id, transaction_id, **args)
    publisher.publish.assert_called_once()
    artifacts = db.list_document_artifacts(workflow_id)
    assert len([a for a in artifacts if a["artifact_type"] == "network_publish_payload"]) == 1

    with pytest.raises(PublishConfigError, match="different release"):
        publication_delivery.deliver_publication(
            network_id, transaction_id, db.get_document(workflow_id)["id"], "another-workflow", "advisory",
            None, None,
        )
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE publication_deliveries SET request_identity = request_identity - 'publication_policy_sha256' "
            "WHERE network_id = %s AND transaction_id = %s", (network_id, transaction_id),
        )
    with pytest.raises(ApplicationError, match="policy changed or was not recorded"):
        await activities.publish_catalog_to_network(workflow_id, transaction_id, **args)
    publisher.publish.assert_called_once()


@pytest.mark.db
@pytest.mark.asyncio
async def test_automatic_release_activates_from_recorded_receipt_after_database_error(
    db_connection, sample_document, monkeypatch,
):
    from unittest.mock import AsyncMock

    db = db_connection
    workflow_id = sample_document["workflow_id"]
    doc = db.get_document(workflow_id)
    network_id = "test.local"
    transaction_id, revision_id = str(uuid4()), str(uuid4())
    collection = "test-published"
    for key, value in {
        "ENABLE_PUBLISH_TO_NETWORK": "true",
        "DISCOVERY_NETWORK_ID": network_id,
        "DISCOVERY_SERVICE_ENDPOINT": "http://discovery.test",
        "NETWORK_SENDER_ID": "provider.test.local",
        "AUTO_RELEASE_NETWORK_ID": network_id,
        "AUTO_RELEASE_COLLECTION": collection,
    }.items():
        monkeypatch.setenv(key, value)
    db.update_document_job(doc["latest_job_id"], config_json={"release_network_id": network_id})
    provider_id = get_network_adapter(network_id).provider_id(doc["kind"], doc["instance"])
    db.save_revision_snapshot(
        doc["id"], workflow_id, network_id, revision_id, {"metadata": {"kind": doc["kind"]}, "passages": []},
        collection=collection, provider_id=provider_id,
    )
    monkeypatch.setattr(db, "get_document_index_status", lambda *_: {
        "status": "indexed", "chunk_count_indexed": 1, "doc_id": revision_id,
    })
    monkeypatch.setattr(activities, "_drop_superseded_points", AsyncMock())
    monkeypatch.setattr(activities, "_upload_artifact", lambda *_: (
        "blob://docs/exchange.json", 10, "application/json",
    ))
    publisher = Mock()
    publisher.publish.return_value = PublicationReceipt(
        exchange={"result_status": "ACCEPTED"}, result_status="ACCEPTED", context_matches=True,
    )
    monkeypatch.setattr(BecknNetworkPublisher, "publish", publisher.publish)

    with monkeypatch.context() as changed:
        changed.setenv("NETWORK_PROVIDER_ID_TEMPLATE", "new-{instance}.{network_id}")
        with pytest.raises(ApplicationError, match="no longer matches the staged release"):
            await activities.publish_catalog_to_network(workflow_id, transaction_id, network_id)
    publisher.publish.assert_not_called()

    activate = db.activate_network_release
    with monkeypatch.context() as attempt:
        attempt.setattr(db, "activate_network_release", Mock(side_effect=OSError("database disconnected")))
        with pytest.raises(OSError, match="database disconnected"):
            await activities.publish_catalog_to_network(workflow_id, transaction_id, network_id)
    hit = {"revision_id": revision_id, "release_network_id": network_id}
    assert db.visible_release_hits([hit]) == []

    assert db.activate_network_release is activate
    await activities.publish_catalog_to_network(workflow_id, transaction_id, network_id)
    assert db.visible_release_hits([hit]) == [hit]
    publisher.publish.assert_called_once()


@pytest.mark.db
def test_managed_publication_validates_wire_attributes_against_network_pack(
    db_connection, sample_document, monkeypatch, tmp_path,
):
    import json

    from pipeline.network.catalog.catalog_profile import load_catalog_profile
    from pipeline.metadata.schema_packs import SchemaPack, install_schema_pack

    db = db_connection
    workflow_id = sample_document["workflow_id"]
    document_id = db.get_document(workflow_id)["id"]
    entrypoint = "https://schemas.example.test/advisory.yaml"
    pack = SchemaPack(
        capability="openagrinet:KnowledgeAdvisory", version="v1.0", entrypoint=entrypoint,
        component="Advisory", schemas={entrypoint: {"components": {"schemas": {
            "Advisory": {
                "type": "object", "required": ["informationMode"],
                "properties": {"informationMode": {"const": "OnDemand"}},
            },
        }}}},
    )
    with db.get_connection() as conn:
        install_schema_pack(conn, "test.local", pack)

    profile = load_catalog_profile()["advisory"].model_dump()
    profile["schema_pack"] = {"capability": pack.capability, "digest": pack.digest}
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"advisory": profile}))
    monkeypatch.setenv("BECKN_CATALOG_PROFILE_PATH", str(profile_path))
    publisher = Mock()
    publisher.publish.return_value = PublicationReceipt(
        exchange={"result_status": "ACCEPTED"}, result_status="ACCEPTED", context_matches=True,
    )
    monkeypatch.setattr(BecknNetworkPublisher, "publish", publisher.publish)

    monkeypatch.setenv("DISCOVERY_NETWORK_ID", "other.local")
    monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "http://discovery.test")
    monkeypatch.setenv("NETWORK_SENDER_ID", "provider.test.local")
    with pytest.raises(PublishConfigError, match="pinned schema pack"):
        publication_delivery.deliver_publication(
            "other.local", str(uuid4()), document_id, workflow_id, "advisory", None, None,
        )
    publisher.publish.assert_not_called()

    from types import SimpleNamespace

    from pipeline.network import publication_admission

    approvals = tmp_path / "approvals.json"
    approvals.write_text(json.dumps([{
        "network_id": "test.local", "participant_id": "bv.test.local",
        "capability": pack.capability, "action": "select", "base_url": "http://provider:8000",
        "method": "POST", "path": "/search", "mapping_url": "https://example.test/advisory.select.yaml",
        "mapping_sha256": "a" * 64, "mapping_runtime": "jsonata@2.1.0", "auth_profile": "provider-search",
    }]))
    for key, value in {
        "NETWORK_CONNECTIONS": "test", "NETWORK_CONNECTION_DEFAULT": "test",
        "NETWORK_CONNECTION_TEST_TYPE": "beckn", "NETWORK_CONNECTION_TEST_ID": "test.local",
        "NETWORK_CONNECTION_TEST_SENDER_ID": "bv.test.local",
        "NETWORK_CONNECTION_TEST_DISCOVERY_URL": "http://discovery:8080",
        "NETWORK_CONNECTION_TEST_APPROVED_ROUTES_FILE": str(approvals),
    }.items():
        monkeypatch.setenv(key, value)
    registry = Mock()
    registry.ready_route.return_value = SimpleNamespace(digest="b" * 64)
    monkeypatch.setattr(publication_admission, "registry_for_network", lambda _: registry)
    transaction_id = str(uuid4())
    receipt = publication_delivery.deliver_publication(
        "test.local", transaction_id, document_id, workflow_id, "advisory", None, None,
    )
    assert receipt.result_status == "ACCEPTED"
    assert receipt.exchange["binding_evidence_sha256"] == "b" * 64
    registry.ready_route.assert_called_once()
    publisher.publish.assert_called_once()
    registry.ready_route.return_value = SimpleNamespace(digest="c" * 64)
    with pytest.raises(PublishConfigError, match="changed after the publication receipt"):
        publication_delivery.deliver_publication(
            "test.local", transaction_id, document_id, workflow_id, "advisory", None, None,
        )
    publisher.publish.assert_called_once()
