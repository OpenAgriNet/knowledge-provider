"""Verify Sunbird participant admission and route evidence against wire records."""

import json
from dataclasses import FrozenInstanceError, replace

import httpx
import pytest

from pipeline.network.network_connections import NetworkConnection
from pipeline.network.network_registry import (
    ApprovedRoute,
    RegistryLookupError,
    SunbirdRegistry,
    registry_for_network,
)


@pytest.fixture
def approval():
    return ApprovedRoute(
        network_id="example.net",
        participant_id="provider.example.net",
        capability="openagrinet:KnowledgeAdvisory",
        action="select",
        base_url="http://knowledge-provider-api:8001",
        method="POST",
        path="/search",
        mapping_url="https://mapping.example.net/v1/advisory.select.yaml",
        mapping_sha256="a" * 64,
        mapping_runtime="jsonata@1.5.4",
        auth_profile="provider-select",
        retry_max=1,
    )


@pytest.fixture
def participant(approval):
    return {
        "osid": "participant-row",
        "osUpdatedAt": "2026-11-12T10:00:00Z",
        "participantId": approval.participant_id,
        "name": "Knowledge Provider",
        "type": "upstream",
        "status": "active",
        "baseUrl": approval.base_url,
    }


@pytest.fixture
def binding(approval):
    return {
        "osid": "binding-row",
        "osUpdatedAt": "2026-11-12T10:01:00Z",
        "bindingKey": approval.binding_key,
        "participantId": approval.participant_id,
        "capabilityCode": approval.capability,
        "status": "active",
        "actions": [{
            "osid": "select-action-row",
            "action": approval.action,
            "method": approval.method,
            "path": approval.path,
            "mappings": approval.mapping_url,
            "timeoutMs": approval.timeout_ms,
            "retryMax": approval.retry_max,
            "status": "active",
        }],
    }


@pytest.fixture
def registry(monkeypatch, approval, participant, binding):
    replies = {"Participant": [participant], "ProviderSchema": [binding]}
    calls = []

    def search(request):
        assert request.method == "POST"
        assert request.url.host == "registry.example.net"
        entity = request.url.path.split("/")[-2]
        assert request.url.path == f"/api/v1/{entity}/search"
        calls.append((entity, json.loads(request.content)["filters"]))
        payload = replies[entity]
        if isinstance(payload, httpx.Response):
            return payload
        if entity == "Participant":
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"params": {"status": "SUCCESSFUL"}, "data": payload})

    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **options: client(transport=httpx.MockTransport(search), **options))
    connection = NetworkConnection(
        adapter="beckn",
        network_id=approval.network_id,
        discovery_endpoint="https://discovery.example.net",
        sender_id=approval.participant_id,
        receiver_id="network.example.net",
        visible_to=(),
        registry_url="https://registry.example.net/api/v1/",
    )
    return SunbirdRegistry(connection), replies, calls


def test_observation_includes_inactive_records_and_uses_creation_revision(registry, approval, participant, binding):
    client, _, _ = registry
    participant["status"] = "inactive"
    participant["osCreatedAt"] = participant.pop("osUpdatedAt")
    binding["actions"][0]["status"] = "inactive"

    observed = client.observe_participant(approval.participant_id)
    assert observed.status == "inactive"
    assert observed.revision == participant["osCreatedAt"]
    assert observed.record_id == participant["osid"]
    route = client.observe_binding(approval.participant_id, approval.capability)
    assert route.revision == binding["osUpdatedAt"]
    assert route.actions[0].status == "inactive"
    assert route.actions[0].mapping_url == approval.mapping_url


def test_observation_distinguishes_absence_from_ambiguous_registry_state(registry, approval, binding):
    client, replies, _ = registry
    replies["Participant"] = []
    assert client.observe_participant(approval.participant_id) is None

    binding["actions"].append(binding["actions"][0].copy())
    with pytest.raises(RegistryLookupError) as error:
        client.observe_binding(approval.participant_id, approval.capability)
    assert (error.value.entity, error.value.reason) == ("ActionBinding", "duplicate")


def test_approved_route_produces_immutable_network_scoped_evidence(registry, approval, binding):
    client, _, calls = registry
    evidence = client.ready_route(approval)
    assert calls == [
        ("Participant", {"participantId": {"eq": approval.participant_id}}),
        ("ProviderSchema", {"bindingKey": {"eq": approval.binding_key}}),
    ]
    assert evidence.admission.network_id == approval.network_id
    assert evidence.admission.registry_url == "https://registry.example.net/api/v1"
    assert evidence.admission.record_id == "participant-row"
    assert evidence.binding_record_id == "binding-row"
    assert evidence.action_record_id == "select-action-row"
    assert evidence.approval.mapping_sha256 == approval.mapping_sha256
    with pytest.raises(FrozenInstanceError):
        evidence.admission.base_url = "https://other.example.net"

    assert client.ready_route(approval).digest == evidence.digest
    assert client.ready_route(replace(approval, mapping_sha256="b" * 64)).digest != evidence.digest
    binding["osUpdatedAt"] = "2026-11-12T10:02:00Z"
    assert client.ready_route(approval).digest != evidence.digest
    assert evidence.binding_updated_at == "2026-11-12T10:01:00Z"


@pytest.mark.parametrize("entity", ["Participant", "ProviderSchema", "ActionBinding"])
@pytest.mark.parametrize("reason", ["missing", "duplicate", "inactive"])
def test_denials_distinguish_registry_state(registry, approval, binding, entity, reason):
    client, replies, _ = registry
    rows = binding["actions"] if entity == "ActionBinding" else replies[entity]
    if reason == "missing":
        rows.clear()
    elif reason == "duplicate":
        rows.append({**rows[0], "status": "inactive"})
    else:
        rows[0]["status"] = "inactive"
    with pytest.raises(RegistryLookupError) as error:
        client.ready_route(approval)
    assert (error.value.entity, error.value.reason) == (entity, reason)


def test_network_mismatch_stops_before_registry_search(registry, approval):
    client, _, calls = registry
    with pytest.raises(RegistryLookupError) as error:
        client.ready_route(replace(approval, network_id="other.net"))
    assert (error.value.entity, error.value.reason) == ("network", "mismatch")
    assert not calls


def test_configured_connection_uses_the_requested_network_registry(monkeypatch, participant):
    monkeypatch.setenv("NETWORK_CONNECTIONS", "alpha,beta")
    monkeypatch.setenv("NETWORK_CONNECTION_DEFAULT", "alpha")
    for name in ("alpha", "beta"):
        prefix = f"NETWORK_CONNECTION_{name.upper()}_"
        monkeypatch.setenv(prefix + "TYPE", "beckn")
        monkeypatch.setenv(prefix + "ID", f"{name}.example.net")
        monkeypatch.setenv(prefix + "DISCOVERY_URL", f"https://discovery.{name}.example.net")
        monkeypatch.setenv(prefix + "REGISTRY_URL", f"https://registry.{name}.example.net")
        monkeypatch.setenv(prefix + "SENDER_ID", f"sender.{name}.example.net")
        monkeypatch.delenv(prefix + "REGISTRY_TOKEN_FILE", raising=False)

    def search(request):
        assert request.url.host == "registry.beta.example.net"
        return httpx.Response(200, json={"data": [participant]})

    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **options: client(transport=httpx.MockTransport(search), **options))
    registry = registry_for_network("beta.example.net")
    assert registry.active_provider(participant["participantId"]).network_id == "beta.example.net"


@pytest.mark.parametrize(
    ("entity", "field", "value"),
    [
        ("Participant", "type", "node"),
        ("Participant", "baseUrl", "https://other.example.net"),
        ("ProviderSchema", "participantId", "other.example.net"),
        ("ProviderSchema", "capabilityCode", "openagrinet:Other"),
        ("ActionBinding", "method", "GET"),
        ("ActionBinding", "path", "/different"),
        ("ActionBinding", "mappings", "https://other.example.net/advisory.select.yaml"),
        ("ActionBinding", "timeoutMs", 20_000),
        ("ActionBinding", "retryMax", 0),
    ],
)
def test_record_must_match_approved_identity_and_route(registry, approval, binding, entity, field, value):
    client, replies, _ = registry
    row = binding["actions"][0] if entity == "ActionBinding" else replies[entity][0]
    row[field] = value
    with pytest.raises(RegistryLookupError) as error:
        client.ready_route(approval)
    assert (error.value.entity, error.value.reason) == (entity, "mismatch")


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(503), "unavailable"),
        (httpx.Response(302, headers={"Location": "https://other.example.net/Participant/search"}), "unavailable"),
        (httpx.Response(200, json={"params": {"status": "UNSUCCESSFUL"}, "data": []}), "unavailable"),
        (httpx.Response(200, json={"unknown": []}), "invalid"),
        (httpx.Response(200, json=[{"participantId": "wrong.provider.net"}]), "invalid"),
        (httpx.Response(200, content=b"x" * (128 * 1024 + 1)), "invalid"),
    ],
)
def test_registry_failure_does_not_read_as_missing(registry, approval, response, reason):
    client, replies, calls = registry
    replies["Participant"] = response
    with pytest.raises(RegistryLookupError) as error:
        client.active_provider(approval.participant_id)
    assert (error.value.entity, error.value.reason) == ("Participant", reason)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mapping_sha256", "not-a-digest"),
        ("mapping_runtime", "jsonata@latest"),
        ("auth_profile", ""),
        ("base_url", "https://host.example.net?token=unapproved"),
        ("path", "/service/../secret"),
    ],
)
def test_approval_requires_a_well_formed_route_and_immutable_mapping_pin(approval, field, value):
    with pytest.raises(ValueError):
        replace(approval, **{field: value})
