"""Local Registry proposals and decisions, separate from network admission reads."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from psycopg.types.json import Jsonb

from pipeline import db
from pipeline.network.network_connections import configured_network_connections, network_connection
from pipeline.network.network_group import load_provider_naming, named_group
from pipeline.network.network_registry import (
    ApprovedRoute,
    BindingObservation,
    ParticipantObservation,
    _valid_url,
    registry_for_network,
)

DesiredStatus = Literal["active", "inactive"]
DecisionOutcome = Literal["approved", "rejected"]


class RegistryProposalConflict(RuntimeError):
    """A stale proposal or a retry that differs from the original."""


def _bounded_text(value: str, maximum: int, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"Invalid Registry proposal {field}")


def _check_status(status: DesiredStatus) -> None:
    if status not in ("active", "inactive"):
        raise ValueError("A Registry proposal must request active or inactive status")


@dataclass(frozen=True)
class RegistryActor:
    """Principal and scopes resolved by the caller's trusted authorization layer."""

    principal_id: str
    managed_provider_ids: frozenset[UUID] = frozenset()
    operator_network_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _bounded_text(self.principal_id, 200, "actor")
        object.__setattr__(self, "managed_provider_ids", frozenset(self.managed_provider_ids))
        object.__setattr__(self, "operator_network_ids", frozenset(self.operator_network_ids))


@dataclass(frozen=True)
class RegistryRecordVersion:
    """Observed remote row and revision to reconcile before any future write."""

    record_id: str
    revision: str

    def __post_init__(self) -> None:
        _bounded_text(self.record_id, 253, "record ID")
        _bounded_text(self.revision, 253, "record revision")


@dataclass(frozen=True)
class AdmissionChange:
    name: str
    base_url: str
    status: DesiredStatus

    def __post_init__(self) -> None:
        _bounded_text(self.name, 200, "participant name")
        if not _valid_url(self.base_url):
            raise ValueError("Invalid Registry participant base URL")
        _check_status(self.status)


@dataclass(frozen=True)
class RouteChange:
    route: ApprovedRoute
    schema_pack_digest: str
    status: DesiredStatus

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.schema_pack_digest):
            raise ValueError("A route proposal requires a schema pack digest")
        _check_status(self.status)


@dataclass(frozen=True)
class RegistryDecision:
    outcome: DecisionOutcome
    decided_by: str
    reason: str
    decided_at: datetime


@dataclass(frozen=True)
class RegistryProposal:
    id: UUID
    request_id: UUID
    network_id: str
    provider_id: UUID
    participant_id: str
    version: int
    change: AdmissionChange | RouteChange
    observed_record: RegistryRecordVersion | None
    submitted_by: str
    submission_reason: str
    submitted_at: datetime
    decision: RegistryDecision | None


@dataclass(frozen=True)
class RegistryNetwork:
    network_id: str
    participant_id: str


@dataclass(frozen=True)
class RegistryStream:
    expected_version: int
    current: ParticipantObservation | BindingObservation | None
    latest_proposal: RegistryProposal | None


@dataclass(frozen=True)
class RegistryContext:
    network_id: str
    participant_id: str
    schema_packs: list[dict]
    admission: RegistryStream
    route: RegistryStream | None


_PROPOSALS = """
    SELECT p.*, s.participant_id, d.outcome, d.decided_by, d.reason AS decision_reason, d.decided_at
    FROM registry_operator_proposals p
    JOIN registry_operator_subjects s USING (network_id, provider_id)
    LEFT JOIN registry_operator_decisions d ON d.proposal_id = p.id
"""


def _snapshot(row: dict) -> RegistryProposal:
    data = row["change"]
    change = (
        RouteChange(ApprovedRoute(**data["route"]), row["schema_pack_digest"], data["status"])
        if row["capability"] else AdmissionChange(**data)
    )
    return RegistryProposal(
        id=row["id"], request_id=row["request_id"], network_id=row["network_id"],
        provider_id=row["provider_id"], participant_id=row["participant_id"],
        version=row["version"], change=change,
        observed_record=(
            RegistryRecordVersion(row["observed_record_id"], row["observed_revision"])
            if row["observed_record_id"] is not None else None
        ),
        submitted_by=row["submitted_by"], submission_reason=row["submission_reason"],
        submitted_at=row["submitted_at"],
        decision=(
            RegistryDecision(row["outcome"], row["decided_by"], row["decision_reason"], row["decided_at"])
            if row["outcome"] is not None else None
        ),
    )


def _proposal_row(conn, network_id: str, proposal_id: UUID) -> dict:
    row = conn.execute(
        _PROPOSALS + " WHERE p.network_id = %s AND p.id = %s",
        (network_id, proposal_id),
    ).fetchone()
    if row is None:
        raise LookupError("Registry proposal not found")
    return row


def _locked_subject(conn, network_id: str, provider_id: UUID) -> dict | None:
    return conn.execute(
        """
        SELECT participant_id FROM registry_operator_subjects
        WHERE network_id = %s AND provider_id = %s FOR UPDATE
        """,
        (network_id, provider_id),
    ).fetchone()


def _current_version(conn, network_id: str, provider_id: UUID, capability: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(version), 0) AS version FROM registry_operator_proposals
        WHERE network_id = %s AND provider_id = %s AND capability = %s
        """,
        (network_id, provider_id, capability),
    ).fetchone()
    return row["version"]


def _provider_name(conn, provider_id: UUID) -> str:
    provider = conn.execute("SELECT name FROM instances WHERE id = %s", (provider_id,)).fetchone()
    if provider is None:
        raise LookupError("Provider not found")
    return provider["name"]


def _participant_id(network_id: str, provider: str) -> str:
    return load_provider_naming(network_id).provider(named_group(provider))["id"]


def _require_registry_manager(actor: RegistryActor, provider_id: UUID) -> None:
    if provider_id not in actor.managed_provider_ids:
        raise PermissionError("Provider manager access is required")


def list_provider_registry_networks(*, actor: RegistryActor, provider_id: UUID) -> list[RegistryNetwork]:
    _require_registry_manager(actor, provider_id)
    with db.get_connection() as conn:
        provider = _provider_name(conn, provider_id)
    return [
        RegistryNetwork(connection.network_id, _participant_id(connection.network_id, provider))
        for connection in configured_network_connections()
        if connection.adapter == "beckn" and connection.registry_url
    ]


def _latest_proposal(conn, network_id: str, provider_id: UUID, capability: str) -> RegistryProposal | None:
    row = conn.execute(
        _PROPOSALS + """
        WHERE p.network_id = %s AND p.provider_id = %s AND p.capability = %s
        ORDER BY p.version DESC LIMIT 1
        """,
        (network_id, provider_id, capability),
    ).fetchone()
    return _snapshot(row) if row else None


def read_registry_context(
    *, actor: RegistryActor, network_id: str, provider_id: UUID, capability: str | None,
) -> RegistryContext:
    """Pair current Registry observations with the local proposal streams."""
    _require_registry_manager(actor, provider_id)
    connection = network_connection(network_id)
    if connection.adapter != "beckn" or not connection.registry_url:
        raise ValueError("Registry context requires a configured Registry connection")
    with db.get_connection() as conn:
        participant = _participant_id(network_id, _provider_name(conn, provider_id))
        packs = conn.execute(
            """
            SELECT capability, digest, version FROM schema_pack_versions WHERE network_id = %s
            ORDER BY capability, installed_at DESC, digest
            """,
            (network_id,),
        ).fetchall()
        admission = _latest_proposal(conn, network_id, provider_id, "")
        route = _latest_proposal(conn, network_id, provider_id, capability) if capability else None
        if any(proposal and proposal.participant_id != participant for proposal in (admission, route)):
            raise RegistryProposalConflict("The configured participant differs from the existing proposal stream")

    registry = registry_for_network(network_id)
    current_admission = registry.observe_participant(participant)
    current_route = registry.observe_binding(participant, capability) if capability else None
    return RegistryContext(
        network_id, participant, packs,
        RegistryStream(admission.version if admission else 0, current_admission, admission),
        RegistryStream(route.version if route else 0, current_route, route) if capability else None,
    )


def submit_registry_proposal(
    *, actor: RegistryActor, network_id: str, provider_id: UUID, request_id: UUID,
    expected_version: int, change: AdmissionChange | RouteChange,
    observed_record: RegistryRecordVersion | None, reason: str,
) -> RegistryProposal:
    """Append an intended change. A null observed record expresses a proposed create."""
    _require_registry_manager(actor, provider_id)
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("The expected Registry proposal version must be nonnegative")
    _bounded_text(reason, 1000, "submission reason")
    reason = reason.strip()
    if change.status == "inactive" and observed_record is None:
        raise ValueError("An inactive proposal requires an observed Registry record")
    connection = network_connection(network_id)
    if connection.adapter != "beckn" or not connection.registry_url:
        raise ValueError("Registry proposals require a configured Registry connection")

    with db.get_connection() as conn:
        participant_id = _participant_id(network_id, _provider_name(conn, provider_id))
        if isinstance(change, RouteChange):
            if (change.route.network_id, change.route.participant_id) != (network_id, participant_id):
                raise ValueError("The route does not match the configured provider identity")
            capability = change.route.capability
            pack_digest = change.schema_pack_digest
            payload = {"route": asdict(change.route), "status": change.status}
        else:
            capability, pack_digest, payload = "", None, asdict(change)

        conn.execute(
            """
            INSERT INTO registry_operator_subjects (network_id, provider_id, participant_id)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
            """,
            (network_id, provider_id, participant_id),
        )
        subject = _locked_subject(conn, network_id, provider_id)
        if subject is None or subject["participant_id"] != participant_id:
            raise RegistryProposalConflict("The public participant identity belongs to another Registry subject")

        prior = conn.execute(
            _PROPOSALS + " WHERE p.network_id = %s AND p.provider_id = %s AND p.request_id = %s",
            (network_id, provider_id, request_id),
        ).fetchone()
        if prior is not None:
            proposal = _snapshot(prior)
            if (proposal.version - 1, proposal.change, proposal.observed_record, proposal.submitted_by,
                    proposal.submission_reason) != (
                expected_version, change, observed_record, actor.principal_id, reason,
            ):
                raise RegistryProposalConflict("The request ID already identifies a different proposal")
            return proposal
        if _current_version(conn, network_id, provider_id, capability) != expected_version:
            raise RegistryProposalConflict("The Registry proposal stream has a newer version")
        if pack_digest is not None and conn.execute(
            "SELECT 1 FROM schema_pack_versions WHERE network_id = %s AND capability = %s AND digest = %s",
            (network_id, capability, pack_digest),
        ).fetchone() is None:
            raise ValueError("The proposed route's schema pack is not installed for this network")

        row = conn.execute(
            """
            INSERT INTO registry_operator_proposals (
              network_id, provider_id, request_id, capability, version, schema_pack_digest, change,
              observed_record_id, observed_revision, submitted_by, submission_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (network_id, provider_id, request_id, capability, expected_version + 1, pack_digest, Jsonb(payload),
             observed_record.record_id if observed_record else None,
             observed_record.revision if observed_record else None,
             actor.principal_id, reason),
        ).fetchone()
        return _snapshot(_proposal_row(conn, network_id, row["id"]))


def read_registry_proposal(*, actor: RegistryActor, network_id: str, proposal_id: UUID) -> RegistryProposal:
    with db.get_connection() as conn:
        row = _proposal_row(conn, network_id, proposal_id)
        if network_id not in actor.operator_network_ids and row["provider_id"] not in actor.managed_provider_ids:
            raise LookupError("Registry proposal not found")
        return _snapshot(row)


def decide_registry_proposal(
    *, actor: RegistryActor, network_id: str, proposal_id: UUID, expected_version: int,
    outcome: DecisionOutcome, reason: str,
) -> RegistryProposal:
    """Record one local operator decision about the latest proposal version."""
    if network_id not in actor.operator_network_ids:
        raise PermissionError("Operator access for this network is required")
    if type(expected_version) is not int or expected_version < 1:
        raise ValueError("The expected Registry proposal version must be positive")
    if outcome not in ("approved", "rejected"):
        raise ValueError("The Registry proposal decision must be approved or rejected")
    _bounded_text(reason, 1000, "decision reason")
    reason = reason.strip()
    with db.get_connection() as conn:
        target = _proposal_row(conn, network_id, proposal_id)
        provider_id = target["provider_id"]
        # Serialize both decisions and new proposals for this provider/network.
        _locked_subject(conn, network_id, provider_id)
        row = _proposal_row(conn, network_id, proposal_id)
        proposal = _snapshot(row)
        if proposal.version != expected_version:
            raise RegistryProposalConflict("The decision version does not match the proposal")
        if proposal.decision is not None:
            decision = proposal.decision
            if (decision.outcome, decision.decided_by, decision.reason) != (outcome, actor.principal_id, reason):
                raise RegistryProposalConflict("The Registry proposal already has a different decision")
            return proposal
        if _current_version(conn, network_id, provider_id, row["capability"]) != expected_version:
            raise RegistryProposalConflict("The Registry proposal was superseded before the decision")
        conn.execute(
            """
            INSERT INTO registry_operator_decisions (proposal_id, outcome, decided_by, reason)
            VALUES (%s, %s, %s, %s)
            """,
            (proposal_id, outcome, actor.principal_id, reason),
        )
        return _snapshot(_proposal_row(conn, network_id, proposal_id))


def list_network_proposals(
    *, actor: RegistryActor, network_id: str, pending_only: bool = True, limit: int = 50,
) -> list[RegistryProposal]:
    """Return recent local proposals within the operator's network."""
    if network_id not in actor.operator_network_ids:
        raise PermissionError("Operator access for this network is required")
    with db.get_connection() as conn:
        rows = conn.execute(
            _PROPOSALS + """
            WHERE p.network_id = %s AND (
                NOT %s OR (
                    d.proposal_id IS NULL AND NOT EXISTS (
                        SELECT 1 FROM registry_operator_proposals newer
                        WHERE newer.network_id = p.network_id AND newer.provider_id = p.provider_id
                          AND newer.capability = p.capability AND newer.version > p.version
                    )
                )
            )
            ORDER BY p.submitted_at DESC, p.id DESC LIMIT %s
            """,
            (network_id, pending_only, max(1, min(limit, 100))),
        ).fetchall()
    return [_snapshot(row) for row in rows]
