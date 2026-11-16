import asyncio
import copy
import json

import pytest

from pipeline.api import app
from pipeline.api.routers import webhook_sources
from pipeline.auth.deps import get_current_user
from pipeline.auth.models import AuthUser
from pipeline.intake.source_intake import (
    NormalizedSourceDocument,
    materialize_intake_pages,
    reconcile_pending_sources,
)
from pipeline.intake.webhook_mapping import WebhookMapping, map_webhook
from tests.icar_webhook_fixture import bulk_payload

MAPPING = {
    "records": "/popList",
    "each_root": True,
    "fields": {
        "external_id": "/popID", "title": {"scope": "root", "path": "/title"},
        "text": "/advisory", "language": "/language", "translations": "/translations",
        "crop_subjects": {"scope": "root", "path": "/crops"},
        "knowledge_metadata": {"scope": "root", "path": "/metadata"},
    },
    "filters": [
        {"scope": "root", "path": "/isPublished", "equals": 1},
        {"path": "/status", "equals": "Published"},
    ],
}

PAYLOAD = {
    "title": "Sorghum field note", "newTitle": "Revised sorghum field note", "isPublished": 1,
    "crops": [{"subjectId": "https://example.test/taxonomy/sorghum"}],
    "metadata": {"subject_categories": ["Crop"], "seasons": ["Rabi"]},
    "popList": [
        {"popID": 987, "status": "Published", "advisory": "જુવારને પાણી આપો.",
         "language": "gu", "translations": {"en": "Water sorghum."}},
        {"popID": 900, "status": "Draft", "advisory": "Private draft."},
    ],
}


def test_mapping_normalizes_one_published_nested_entry():
    _, advisory = map_webhook(PAYLOAD, WebhookMapping.model_validate(MAPPING))[0]

    assert advisory.external_id == "987"
    assert advisory.title == PAYLOAD["title"]
    assert advisory.language == "gu"
    assert advisory.translations == {"en": "Water sorghum."}
    assert advisory.model_dump(mode="json")["translations"] == {"en": "Water sorghum."}
    assert advisory.crop_subjects[0].subjectId == PAYLOAD["crops"][0]["subjectId"]
    assert advisory.knowledge_metadata.storage_value() == PAYLOAD["metadata"]
    assert map_webhook({**PAYLOAD, "isPublished": 0}, WebhookMapping.model_validate(MAPPING)) == []
    assert map_webhook([PAYLOAD], WebhookMapping.model_validate(MAPPING))[0][1] == advisory


@pytest.mark.parametrize("translations", [
    {"fr": "Bonjour", "FR": {"text": "Bonjour"}},
    {"en": {"text": "The source language"}},
    {"fr": {"text": "   ", "title": "Titre"}},
    {"fr": {"text": "Bonjour", "unsupported": "field"}},
    {"fr": {"text": "Bonjour", "title": "Conflicting title"}},
])
def test_structured_translations_reject_invalid_or_conflicting_values(translations):
    with pytest.raises(ValueError):
        NormalizedSourceDocument.model_validate({
            "external_id": "item", "title": "Title", "text": "Original", "language": "en",
            "translations": translations,
            "knowledge_metadata": {"localized": {"fr": {"title": "Titre"}}},
        })


def test_outer_array_maps_all_published_legacy_fixture_entries():
    payload = bulk_payload()
    withheld_parent = {**payload, "isPublished": 0, "popList": payload["popList"][:1]}
    documents = map_webhook([payload, withheld_parent], WebhookMapping.model_validate(MAPPING))

    assert len(documents) == 205
    assert {document.external_id for _, document in documents} == {str(10_000 + i) for i in range(205)}
    assert all(document.title == payload["title"] for _, document in documents)


@pytest.mark.api
@pytest.mark.db
def test_webhook_policy_receipts_replay_revision_and_revoke(
    test_client, db_connection, mock_temporal_client, mock_object_store, monkeypatch,
):
    db_connection.upsert_instance("/states/MH")
    monkeypatch.setenv("KEYCLOAK_VERIFY_LIVE_GROUPS", "true")
    monkeypatch.setenv("KEYCLOAK_ISSUER", "https://issuer.example.test/realms/demo")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "knowledge-intake")
    monkeypatch.setenv("ENABLE_PUBLISH_TO_NETWORK", "true")
    monkeypatch.setenv("DISCOVERY_NETWORK_ID", "oan-dev.local")
    monkeypatch.setenv("AUTO_RELEASE_NETWORK_ID", "oan-dev.local")
    monkeypatch.setenv("AUTO_RELEASE_COLLECTION", "published-advisories")
    for key, value in {
        "NETWORK_CONNECTIONS": "dev,prod",
        "NETWORK_CONNECTION_DEFAULT": "dev",
        "NETWORK_CONNECTION_DEV_ID": "oan-dev.local",
        "NETWORK_CONNECTION_DEV_TYPE": "beckn",
        "NETWORK_CONNECTION_DEV_SENDER_ID": "publisher.oan-dev.local",
        "NETWORK_CONNECTION_DEV_DISCOVERY_URL": "https://discovery-dev.example.test",
        "NETWORK_CONNECTION_DEV_RELEASE_COLLECTION": "published-advisories",
        "NETWORK_CONNECTION_PROD_ID": "oan-prod.local",
        "NETWORK_CONNECTION_PROD_TYPE": "beckn",
        "NETWORK_CONNECTION_PROD_SENDER_ID": "publisher.oan-prod.local",
        "NETWORK_CONNECTION_PROD_DISCOVERY_URL": "https://discovery-prod.example.test",
        "NETWORK_CONNECTION_PROD_RELEASE_COLLECTION": "prod-advisories",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(webhook_sources._api.limiter, "enabled", False)
    monkeypatch.setattr(webhook_sources.keycloak_admin, "find_webhook_service_subject", lambda _: "fixture-sender")
    monkeypatch.setattr(webhook_sources, "decode_verified_claims", lambda token, config: {
        "typ": "Bearer", "azp": "fixture-client", "sub": token,
    })
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: AuthUser(
        user_id="fixture-admin", roles=["state_admin"], instances=["mh"], state_roles={"mh": "state_admin"},
    ))
    base = "/webhook/sources/mh/field-feed"

    def put(mapping, *, version=0, enabled=True, network=None, auto_release=True):
        return test_client.put(base, json={
            "expected_version": version, "mapping": mapping, "fixture": PAYLOAD,
            "service_client": "fixture-client", "service_subject": "fixture-sender",
            "auto_release": auto_release, "enabled": enabled, "release_network_id": network,
        })

    def send(key, body=PAYLOAD, sender="fixture-sender"):
        return test_client.post(f"{base}/events", json=body, headers={
            "Authorization": f"Bearer {sender}", "Idempotency-Key": key,
        })

    assert put(MAPPING, network="unknown.test").status_code == 422
    disabled_release = put(MAPPING, network="oan-prod.local", auto_release=False)
    assert disabled_release.status_code == 422
    assert disabled_release.json() == {"detail": "A release network requires automatic release"}
    policy = put(MAPPING)
    assert policy.status_code == 200, policy.text
    assert policy.json()["version"] == 1
    assert send("no-access", sender="wrong-subject").status_code == 403
    assert test_client.post(f"{base}/events", json=PAYLOAD, headers={"Idempotency-Key": "no-token"}).status_code == 401

    preview = test_client.post("/knowledge/intake/mh/mapping-preview", json={"mapping": MAPPING, "fixture": PAYLOAD})
    assert preview.status_code == 200, preview.text
    assert [row["external_id"] for row in preview.json()["documents"]] == ["987"]
    mock_object_store.put.assert_not_called()

    mock_temporal_client.start_workflow.side_effect = TimeoutError("start reply lost")
    original = (json.dumps(PAYLOAD, ensure_ascii=False, indent=2) + "\n").encode()
    headers = {"Content-Type": "application/json", "Authorization": "Bearer fixture-sender"}
    mock_object_store.put.side_effect = OSError("temporary storage failure")
    assert test_client.post(f"{base}/events", content=original, headers={
        **headers, "Idempotency-Key": "first-event",
    }).status_code == 503
    mock_temporal_client.start_workflow.assert_not_awaited()
    mock_object_store.put.reset_mock(side_effect=True)
    first = test_client.post(f"{base}/events", content=original, headers={
        **headers, "Idempotency-Key": "first-event",
    })
    assert first.status_code == 202, first.text
    mock_object_store.put.assert_called_once()
    ref, body, size, mime_type = mock_object_store.put.call_args.args
    assert (body.getvalue(), size, mime_type) == (original, len(original), "application/json")
    assert ref.key.startswith("webhook-deliveries/")
    deliveries = "/knowledge/intake/mh/field-feed/deliveries"
    assert test_client.get(deliveries).json()["deliveries"][0]["has_original"] is True
    mock_object_store.open.return_value = iter((original,))
    downloaded = test_client.get(f"{deliveries}/original", params={"delivery_key": "first-event"})
    assert downloaded.content == original
    assert downloaded.headers["cache-control"] == "no-store"
    assert test_client.get(
        "/knowledge/intake/ka/field-feed/deliveries/original", params={"delivery_key": "first-event"},
    ).status_code == 403
    item, = first.json()["items"]
    assert item["status"] == "start_pending"
    assert db_connection.get_document(item["document_id"])["name"] == PAYLOAD["title"]
    assert db_connection.get_document(item["document_id"])["knowledge_metadata"] == PAYLOAD["metadata"]
    job = db_connection.get_latest_document_job(item["document_id"])
    assert job["config"]["release_network_id"] == "oan-dev.local"
    args = job["config"]["upload_start_args"]
    assert args[6:] == ["published-advisories", True, False, True, "oan-dev.local"]
    materialize_intake_pages(item["workflow_id"], args[2])
    page, = db_connection.get_pages(item["document_id"])
    assert page["original_markdown"] == PAYLOAD["popList"][0]["advisory"]
    assert page["translations"]["en"]["translated_markdown"] == "Water sorghum."

    revised_mapping = copy.deepcopy(MAPPING)
    revised_mapping["fields"]["title"]["path"] = "/newTitle"
    second_policy = put(revised_mapping, version=1, network="oan-prod.local")
    assert second_policy.status_code == 200, second_policy.text
    assert second_policy.json()["release_network_id"] == "oan-prod.local"
    assert second_policy.json()["mapping_id"] != first.json()["mapping_version_id"]

    with db_connection.get_connection() as conn:
        conn.execute("UPDATE source_intake_receipts SET created_at = now() - interval '6 seconds' WHERE id = %s::uuid", (item["receipt_id"],))
    queue = job["config"]["source_task_queue"]
    assert asyncio.run(reconcile_pending_sources(mock_temporal_client, queue)) == 0
    assert db_connection.get_document_job(job["id"])["config"]["source_dispatch_attempts"] == 1
    mock_temporal_client.start_workflow.side_effect = None
    assert asyncio.run(reconcile_pending_sources(mock_temporal_client, queue)) == 0
    with db_connection.get_connection() as conn:
        conn.execute(
            "UPDATE workflows SET config = config - 'source_next_dispatch_at' WHERE id = %s::uuid", (job["id"],),
        )
    assert asyncio.run(reconcile_pending_sources(mock_temporal_client, queue)) == 1
    replay = send("first-event")
    assert replay.status_code == 202
    assert replay.json()["mapping_version_id"] == first.json()["mapping_version_id"]
    mock_object_store.put.assert_called_once()
    assert replay.json()["items"] == [{**item, "status": "accepted"}]
    assert db_connection.get_document_job(job["id"])["config"]["release_network_id"] == "oan-dev.local"
    assert mock_temporal_client.start_workflow.call_args.kwargs["id"] == item["workflow_id"]
    assert send("first-event", {**PAYLOAD, "title": "conflict"}).status_code == 409
    assert send("running-revision").status_code == 409

    db_connection.update_document_job(job["id"], status="completed", current_stage="completed")
    with monkeypatch.context() as unavailable:
        unavailable.setenv("NETWORK_CONNECTIONS", "dev")
        assert send("no-destination").status_code == 503
    new_event = send("new-event", {**PAYLOAD, "release_network_id": "oan-dev.local"})
    assert new_event.status_code == 202, new_event.text
    revision, = new_event.json()["items"]
    assert revision["document_id"] == item["document_id"]
    assert revision["workflow_id"] != item["workflow_id"]
    assert db_connection.get_document(item["document_id"])["name"] == PAYLOAD["newTitle"]
    assert new_event.json()["mapping_version_id"] == second_policy.json()["mapping_id"]
    latest = db_connection.get_latest_document_job(item["document_id"])["config"]
    assert latest["release_network_id"] == "oan-prod.local"
    assert latest["release_collection"] == "prod-advisories"
    assert latest["explicit_release_network"]

    assert put(revised_mapping, version=2, enabled=False).status_code == 200
    assert send("first-event").status_code == 403


@pytest.mark.db
def test_named_intake_template_is_pinned_and_canonical_submission_preserves_text(
    test_client, db_connection, monkeypatch, tmp_path,
):
    import json

    db_connection.upsert_instance("/states/MH")
    monkeypatch.setattr(webhook_sources.keycloak_admin, "find_webhook_service_subject", lambda _: "fixture-sender")
    monkeypatch.setitem(app.dependency_overrides, webhook_sources.require_webhook_sender, lambda: (
        "fixture-client", "fixture-sender",
    ))
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: AuthUser(
        user_id="fixture-admin", roles=["state_admin"], instances=["mh"], state_roles={"mh": "state_admin"},
    ))
    monkeypatch.setenv("KEYCLOAK_VERIFY_LIVE_GROUPS", "true")
    template = tmp_path / "intake.json"
    template.write_text(json.dumps({"canonical": {"runtime": "canonical-v1"}}))
    monkeypatch.setenv("KNOWLEDGE_INTAKE_TEMPLATES_FILE", str(template))
    submission = {
        "external_id": "note-1", "title": "Field note", "text": "First paragraph.\n\nSecond paragraph.",
        "kind": "advisory", "language": "en",
        "translations": {"fr-CA": {"text": "Premier paragraphe.", "title": "Note", "description": "Résumé"}},
        "knowledge_metadata": {"localized": {"fr-ca": {"title": "Note"}}, "tags": ["public"]},
    }
    base = "/knowledge/intake/mh/field-feed"
    policy = test_client.put(base, json={
        "template": "canonical", "fixture": submission, "service_client": "fixture-client",
    })
    assert policy.status_code == 200, policy.text
    template.write_text("{}")
    response = test_client.post(f"{base}/submissions", json=submission, headers={"Idempotency-Key": "note"})
    assert response.status_code == 202, response.text
    item, = response.json()["items"]
    job = db_connection.get_latest_document_job(item["document_id"])
    materialize_intake_pages(item["workflow_id"], job["config"]["upload_start_args"][2])
    page, = db_connection.get_pages(item["document_id"])
    assert page["original_markdown"] == submission["text"]
    assert page["translations"]["fr-ca"]["translated_markdown"] == "Premier paragraphe."
    doc = db_connection.get_document(item["document_id"])
    assert doc["knowledge_metadata"] == {
        "localized": {"fr-ca": {"title": "Note", "description": "Résumé"}}, "tags": ["public"],
    }
    with db_connection.get_connection() as conn:
        receipt = conn.execute("SELECT raw_payload, normalized_document FROM source_intake_receipts").fetchone()
    assert receipt["raw_payload"] == submission
    assert receipt["normalized_document"]["translations"] == {"fr-ca": submission["translations"]["fr-CA"]}
    assert test_client.get("/knowledge/intake/schema").json()["anyOf"]
