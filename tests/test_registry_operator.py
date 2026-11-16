"""Database tests for scoped, append-only Registry operator decisions."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from pipeline.network.network_registry import ApprovedRoute
from pipeline.network.registry_operator import (
    AdmissionChange,
    RegistryActor,
    RegistryProposalConflict,
    RegistryRecordVersion,
    RouteChange,
    decide_registry_proposal,
    read_registry_proposal,
    submit_registry_proposal,
)

pytestmark = pytest.mark.db

NETWORK = "dev.example.net"
OTHER_NETWORK = "prod.example.net"
CAPABILITY = "openagrinet:KnowledgeAdvisory"
DIGEST = "a" * 64


@pytest.fixture
def operator_ledger(db_connection, monkeypatch, tmp_path):
    monkeypatch.setenv("NETWORK_CONNECTIONS", "dev,prod")
    monkeypatch.setenv("NETWORK_CONNECTION_DEFAULT", "dev")
    overrides = tmp_path / "provider-ids.json"
    overrides.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PROVIDER_CONFIG_OVERRIDE_PATH", str(overrides))
    monkeypatch.setenv("NETWORK_PROVIDER_ID_TEMPLATE", "{instance}.{network_id}")
    for name, network in (("DEV", NETWORK), ("PROD", OTHER_NETWORK)):
        prefix = f"NETWORK_CONNECTION_{name}_"
        monkeypatch.setenv(prefix + "TYPE", "beckn")
        monkeypatch.setenv(prefix + "ID", network)
        monkeypatch.setenv(prefix + "DISCOVERY_URL", f"https://discovery.{network}")
        monkeypatch.setenv(prefix + "REGISTRY_URL", f"https://registry.{network}")
        monkeypatch.setenv(prefix + "SENDER_ID", f"operator.{network}")

    def unexpected_request(*args, **kwargs):
        pytest.fail("Operator proposals must not call a remote Registry")

    monkeypatch.setattr(httpx, "Client", unexpected_request)
    provider = UUID(db_connection.get_or_create_instance("knowledge-provider"))
    manager = RegistryActor("provider-manager", managed_provider_ids={provider})
    operator = RegistryActor("network-operator", operator_network_ids={NETWORK})
    return db_connection, provider, manager, operator


def admission_args(provider, manager, **overrides):
    return {
        "actor": manager,
        "network_id": NETWORK,
        "provider_id": provider,
        "request_id": uuid4(),
        "expected_version": 0,
        "change": AdmissionChange("Knowledge provider", "https://provider.example.net", "active"),
        "observed_record": None,
        "reason": "Request admission",
        **overrides,
    }


def decision_args(proposal, operator, **overrides):
    return {
        "actor": operator,
        "network_id": proposal.network_id,
        "proposal_id": proposal.id,
        "expected_version": proposal.version,
        "outcome": "approved",
        "reason": "Verified provider ownership",
        **overrides,
    }


def test_idempotent_submission_and_decision_preserve_audit_history(operator_ledger):
    _, provider, manager, operator = operator_ledger
    submitted = admission_args(provider, manager)
    proposal = submit_registry_proposal(**submitted)
    assert proposal.version == 1
    assert proposal.participant_id == f"knowledge-provider.{NETWORK}"
    assert proposal.decision is None
    assert proposal.submitted_by == manager.principal_id
    assert submit_registry_proposal(**submitted) == proposal

    decision = decision_args(proposal, operator)
    approved = decide_registry_proposal(**decision)
    assert approved.decision.outcome == "approved"
    assert approved.decision.decided_by == operator.principal_id
    assert approved.decision.reason == decision["reason"]
    assert approved.decision.decided_at >= approved.submitted_at
    assert decide_registry_proposal(**decision) == approved
    assert submit_registry_proposal(**submitted) == approved
    assert read_registry_proposal(actor=manager, network_id=NETWORK, proposal_id=proposal.id) == approved

    with pytest.raises(RegistryProposalConflict, match="different proposal"):
        submit_registry_proposal(**{**submitted, "reason": "Different request"})
    with pytest.raises(RegistryProposalConflict, match="different decision"):
        decide_registry_proposal(**{**decision, "outcome": "rejected"})


def test_providers_submit_own_records_and_operators_decide_only_their_network(operator_ledger):
    db, provider, manager, operator = operator_ledger
    other = UUID(db.get_or_create_instance("unrelated-provider"))
    with pytest.raises(PermissionError, match="Provider manager"):
        submit_registry_proposal(**admission_args(other, manager))
    proposal = submit_registry_proposal(**admission_args(provider, manager, network_id=OTHER_NETWORK))

    with pytest.raises(PermissionError, match="Operator access"):
        decide_registry_proposal(**decision_args(proposal, operator))
    with pytest.raises(PermissionError, match="Operator access"):
        decide_registry_proposal(**decision_args(proposal, manager))
    with pytest.raises(LookupError, match="not found"):
        read_registry_proposal(actor=operator, network_id=OTHER_NETWORK, proposal_id=proposal.id)
    with pytest.raises(LookupError, match="not found"):
        read_registry_proposal(actor=manager, network_id=NETWORK, proposal_id=proposal.id)


def test_stale_versions_are_not_decided_and_suspension_keeps_observed_revision(operator_ledger):
    _, provider, manager, operator = operator_ledger
    first = submit_registry_proposal(**admission_args(provider, manager))
    with pytest.raises(RegistryProposalConflict, match="newer version"):
        submit_registry_proposal(**admission_args(provider, manager))

    inactive = replace(first.change, status="inactive")
    with pytest.raises(ValueError, match="observed Registry record"):
        submit_registry_proposal(**admission_args(provider, manager, expected_version=1, change=inactive))
    revision = RegistryRecordVersion("participant-row", "2026-11-12T10:00:00Z")
    second = submit_registry_proposal(**admission_args(
        provider, manager, expected_version=1, change=inactive, observed_record=revision,
    ))
    assert second.version == 2
    assert second.observed_record == revision
    with pytest.raises(RegistryProposalConflict, match="superseded"):
        decide_registry_proposal(**decision_args(first, operator))
    rejected = decide_registry_proposal(**decision_args(second, operator, outcome="rejected"))
    assert rejected.decision.outcome == "rejected"
    assert rejected.change.status == "inactive"


def test_route_versions_share_the_capability_binding_and_pin_an_installed_schema(operator_ledger):
    db, provider, manager, operator = operator_ledger
    submit_registry_proposal(**admission_args(provider, manager))
    route = ApprovedRoute(
        network_id=NETWORK,
        participant_id=f"knowledge-provider.{NETWORK}",
        capability=CAPABILITY,
        action="select",
        base_url="https://provider.example.net",
        method="POST",
        path="/search",
        mapping_url="https://mapping.example.net/v1/advisory.select.yaml",
        mapping_sha256="b" * 64,
        mapping_runtime="jsonata@1.5.4",
        auth_profile="provider-select",
    )
    change = RouteChange(route, DIGEST, "active")
    with db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO schema_pack_versions (network_id, capability, digest, version, bundle)
            VALUES (%s, %s, %s, 'v1.0', %s)
            """,
            (OTHER_NETWORK, CAPABILITY, DIGEST, Jsonb({})),
        )
    with pytest.raises(ValueError, match="not installed for this network"):
        submit_registry_proposal(**admission_args(provider, manager, change=change))

    with db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO schema_pack_versions (network_id, capability, digest, version, bundle)
            VALUES (%s, %s, %s, 'v1.0', %s)
            """,
            (NETWORK, CAPABILITY, DIGEST, Jsonb({})),
        )
    with pytest.raises(ValueError, match="configured provider identity"):
        wrong = replace(change, route=replace(route, participant_id=f"unrelated-provider.{NETWORK}"))
        submit_registry_proposal(**admission_args(provider, manager, change=wrong))

    select = submit_registry_proposal(**admission_args(provider, manager, change=change))
    assert select.version == 1
    assert select.change == change

    discover = replace(change, route=replace(
        route, action="discover", mapping_url="https://mapping.example.net/v1/advisory.discover.yaml",
    ))
    update = submit_registry_proposal(**admission_args(provider, manager, expected_version=1, change=discover))
    assert update.version == 2
    assert update.change.route.action == "discover"
    with pytest.raises(RegistryProposalConflict, match="superseded"):
        decide_registry_proposal(**decision_args(select, operator))


def test_concurrent_proposals_cannot_claim_the_same_stream_version(operator_ledger):
    _, provider, manager, _ = operator_ledger
    start = Barrier(2)

    def submit():
        start.wait(timeout=5)
        try:
            return submit_registry_proposal(**admission_args(provider, manager))
        except RegistryProposalConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        proposals = [future.result(timeout=10) for future in futures]
    assert sorted(proposal.version for proposal in proposals if proposal is not None) == [1]


def test_database_rejects_rewriting_decided_history(operator_ledger):
    db, provider, manager, operator = operator_ledger
    proposal = submit_registry_proposal(**admission_args(provider, manager))
    decide_registry_proposal(**decision_args(proposal, operator))

    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"):
        with db.get_connection() as conn:
            conn.execute("UPDATE registry_operator_proposals SET submission_reason = 'rewritten' WHERE id = %s", (
                proposal.id,
            ))
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"):
        with db.get_connection() as conn:
            conn.execute("DELETE FROM registry_operator_decisions WHERE proposal_id = %s", (proposal.id,))
