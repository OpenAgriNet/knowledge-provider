"""Provider submissions and network-scoped decisions for local Registry proposals."""

from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from pipeline import db
from pipeline.api.routers.providers import (
    ProviderCode,
    _require_live_management,
    _require_provider_manager,
)
from pipeline.auth.deps import CurrentUser
from pipeline.auth.groups import ROLE_STATE_ADMIN, provider_group_root
from pipeline.network.network_connections import PublishConfigError
from pipeline.network.network_registry import RegistryLookupError
from pipeline.network.registry_operator import (
    AdmissionChange,
    DecisionOutcome,
    RegistryActor,
    RegistryProposalConflict,
    RegistryRecordVersion,
    RouteChange,
    decide_registry_proposal,
    list_network_proposals,
    list_provider_registry_networks,
    read_registry_context,
    read_registry_proposal,
    submit_registry_proposal,
)

router = APIRouter(tags=["Registry proposals"])
NetworkId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")]
Capability = Annotated[str | None, Query(max_length=128, pattern=r"^openagrinet:[A-Z][A-Za-z0-9]*$")]


class SubmitProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    expected_version: int = Field(ge=0)
    change: AdmissionChange | RouteChange
    observed_record: RegistryRecordVersion | None = None
    reason: str = Field(min_length=1, max_length=1000)


class DecideProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    outcome: DecisionOutcome
    reason: str = Field(min_length=1, max_length=1000)


def _operator_networks(user, network_id: str) -> frozenset[str]:
    if f"/network-operators/{network_id}" in user.groups:
        return frozenset({network_id})
    return frozenset()


def _proposal_response(operation, **arguments):
    try:
        result = operation(**arguments)
        return [asdict(item) for item in result] if isinstance(result, list) else asdict(result)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except RegistryProposalConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except RegistryLookupError as exc:
        raise HTTPException(503, "The current Registry record could not be verified") from exc
    except (ValueError, PublishConfigError) as exc:
        raise HTTPException(422, str(exc)) from exc


def _managed_provider(user, provider: str) -> UUID:
    _require_provider_manager(user, provider)
    group_path = f"/{provider_group_root()}/{provider}"
    with db.get_connection() as conn:
        providers = conn.execute(
            "SELECT id FROM instances WHERE lower(path) = lower(%s) LIMIT 2",
            (group_path,),
        ).fetchall()
    if len(providers) != 1:
        raise HTTPException(404, "Provider not found")
    return providers[0]["id"]


@router.get("/providers/{provider}/registry-networks")
def list_registry_networks(provider: ProviderCode, user: CurrentUser):
    provider_id = _managed_provider(user, provider)
    return {"networks": _proposal_response(
        list_provider_registry_networks,
        actor=RegistryActor(user.user_id, managed_provider_ids=frozenset({provider_id})),
        provider_id=provider_id,
    )}


@router.get("/providers/{provider}/networks/{network_id}/registry-context")
def get_registry_context(provider: ProviderCode, network_id: NetworkId, user: CurrentUser, capability: Capability = None):
    provider_id = _managed_provider(user, provider)
    return _proposal_response(
        read_registry_context,
        actor=RegistryActor(user.user_id, managed_provider_ids=frozenset({provider_id})),
        network_id=network_id, provider_id=provider_id, capability=capability,
    )


@router.post("/providers/{provider}/networks/{network_id}/registry-proposals", status_code=201)
def submit_proposal(provider: ProviderCode, network_id: NetworkId, body: SubmitProposal, user: CurrentUser):
    provider_id = _managed_provider(user, provider)
    return _proposal_response(
        submit_registry_proposal,
        actor=RegistryActor(user.user_id, managed_provider_ids=frozenset({provider_id})),
        provider_id=provider_id, network_id=network_id, request_id=body.request_id,
        expected_version=body.expected_version, change=body.change, observed_record=body.observed_record,
        reason=body.reason,
    )


@router.get("/networks/{network_id}/registry-proposals")
def list_proposals(network_id: NetworkId, user: CurrentUser, pending_only: bool = True, limit: int = Query(50, ge=1, le=100)):
    _require_live_management(user)
    return _proposal_response(
        list_network_proposals,
        actor=RegistryActor(user.user_id, operator_network_ids=_operator_networks(user, network_id)),
        network_id=network_id, pending_only=pending_only, limit=limit,
    )


@router.get("/networks/{network_id}/registry-proposals/{proposal_id}")
def read_proposal(network_id: NetworkId, proposal_id: UUID, user: CurrentUser):
    _require_live_management(user)
    managed = [name for name, role in user.state_roles.items() if role == ROLE_STATE_ADMIN]
    with db.get_connection() as conn:
        providers = conn.execute(
            "SELECT id FROM instances WHERE %s OR lower(name) = ANY(%s)", (user.is_superadmin, managed),
        ).fetchall()
    return _proposal_response(
        read_registry_proposal,
        actor=RegistryActor(
            user.user_id, managed_provider_ids=frozenset(row["id"] for row in providers),
            operator_network_ids=_operator_networks(user, network_id),
        ),
        network_id=network_id, proposal_id=proposal_id,
    )


@router.put("/networks/{network_id}/registry-proposals/{proposal_id}/decision")
def decide_proposal(network_id: NetworkId, proposal_id: UUID, body: DecideProposal, user: CurrentUser):
    _require_live_management(user)
    return _proposal_response(
        decide_registry_proposal, actor=RegistryActor(user.user_id, operator_network_ids=_operator_networks(user, network_id)),
        network_id=network_id, proposal_id=proposal_id, expected_version=body.expected_version,
        outcome=body.outcome, reason=body.reason,
    )
