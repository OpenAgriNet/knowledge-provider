from types import SimpleNamespace
from uuid import uuid4

import pytest

from pipeline.api import app
from pipeline.auth.deps import get_current_user
from pipeline.auth.models import AuthUser
from pipeline.network.network_registry import (
    ActionObservation,
    BindingObservation,
    ParticipantObservation,
    RegistryLookupError,
)


@pytest.mark.db
def test_local_proposal_api_enforces_live_provider_and_network_scopes(test_client, db_connection, monkeypatch):
    monkeypatch.setenv("PORTAL_INSTANCE", "mh")
    monkeypatch.setenv("KEYCLOAK_PROVIDER_GROUP_ROOT", "states")
    db_connection.upsert_instance("/global")
    db_connection.upsert_instance("/providers/MH")
    db_connection.upsert_instance("/states/MH")
    for key, value in {
        "KEYCLOAK_VERIFY_LIVE_GROUPS": "true",
        "NETWORK_CONNECTIONS": "dev",
        "NETWORK_CONNECTION_DEFAULT": "dev",
        "NETWORK_CONNECTION_DEV_TYPE": "beckn",
        "NETWORK_CONNECTION_DEV_ID": "test.dev",
        "NETWORK_CONNECTION_DEV_DISCOVERY_URL": "https://discovery.example.test",
        "NETWORK_CONNECTION_DEV_REGISTRY_URL": "https://registry.example.test",
        "NETWORK_CONNECTION_DEV_SENDER_ID": "provider.test.dev",
    }.items():
        monkeypatch.setenv(key, value)

    def act_as(user):
        monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: user)

    manager = AuthUser(user_id="manager", roles=["state_admin"], instances=["mh"], state_roles={"mh": "state_admin"})
    act_as(manager)
    network_list = test_client.get("/providers/mh/registry-networks")
    assert network_list.status_code == 200
    [network] = network_list.json()["networks"]
    assert network["network_id"] == "test.dev"
    assert test_client.get("/providers/other/registry-networks").status_code == 403

    def observe_participant(participant_id):
        assert participant_id == network["participant_id"]
        return ParticipantObservation("participant-1", "revision-1", "Existing", "https://provider.example.test", "inactive")

    def observe_binding(participant_id, capability):
        assert participant_id == network["participant_id"]
        assert capability == "openagrinet:KnowledgeAdvisory"
        return BindingObservation("binding-1", "revision-2", "active", (
            ActionObservation("select", "POST", "/search", "https://example.test/one.select.yaml", 15000, 0, "inactive"),
        ))

    registry = SimpleNamespace(observe_participant=observe_participant, observe_binding=observe_binding)
    monkeypatch.setattr("pipeline.network.registry_operator.registry_for_network", lambda _: registry)
    context_url = "/providers/mh/networks/test.dev/registry-context"
    context = test_client.get(context_url).json()
    assert context["admission"]["expected_version"] == 0
    assert context["admission"]["current"]["status"] == "inactive"
    assert context["route"] is None
    context = test_client.get(context_url, params={"capability": "openagrinet:KnowledgeAdvisory"}).json()
    assert context["route"]["current"]["actions"][0]["status"] == "inactive"
    assert context["route"]["latest_proposal"] is None

    submission = {
        "request_id": str(uuid4()), "expected_version": 0, "reason": "Onboard provider",
        "change": {"name": "Maharashtra Provider", "base_url": "https://provider.example.test", "status": "active"},
    }
    path = "/providers/mh/networks/test.dev/registry-proposals"
    proposed = test_client.post(path, json=submission)
    assert proposed.status_code == 201, proposed.text
    record = proposed.json()
    assert record["version"] == 1 and record["decision"] is None
    assert test_client.post(path, json=submission).json()["id"] == record["id"]
    context = test_client.get(context_url).json()
    assert context["admission"]["expected_version"] == 1
    assert context["admission"]["latest_proposal"]["id"] == record["id"]
    assert context["admission"]["current"]["status"] == "inactive"
    collection = "/networks/test.dev/registry-proposals"
    decision_url = f"{collection}/{record['id']}/decision"
    decision = {"expected_version": 1, "outcome": "approved", "reason": "Checked desired state"}
    assert test_client.put(decision_url, json=decision).status_code == 403

    act_as(AuthUser(user_id="other-operator", groups=["/network-operators/test.prod"]))
    assert test_client.get(collection).status_code == 403
    assert test_client.get(f"{collection}/{record['id']}").status_code == 404
    assert test_client.put(decision_url, json=decision).status_code == 403

    act_as(manager)
    amended = test_client.post(path, json={**submission, "request_id": str(uuid4()), "expected_version": 1})
    assert amended.status_code == 201, amended.text
    latest = amended.json()
    act_as(AuthUser(user_id="dev-operator", groups=["/network-operators/test.dev"]))
    assert [item["id"] for item in test_client.get(collection).json()] == [latest["id"]]
    assert test_client.put(decision_url, json=decision).status_code == 409
    accepted = test_client.put(
        f"{collection}/{latest['id']}/decision", json={**decision, "expected_version": 2},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["decision"]["outcome"] == "approved"
    assert test_client.get(collection).json() == []
    assert test_client.get(context_url).status_code == 403

    def unavailable(_participant_id):
        raise RegistryLookupError("Participant", "unavailable")

    act_as(manager)
    registry.observe_participant = unavailable
    failure = test_client.get(context_url)
    assert failure.status_code == 503
    assert failure.json()["detail"] == "The current Registry record could not be verified"
