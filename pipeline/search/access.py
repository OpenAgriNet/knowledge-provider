"""Bind serving network selection to a verified, explicitly provisioned principal."""

import json
import os

from fastapi import HTTPException

from pipeline.auth.models import AuthUser
from pipeline.network.release_policy import index_only_mode, managed_release_targets


def _network_grants() -> dict[str, list[str]]:
    grants = json.loads(os.environ.get("SEARCH_NETWORK_PRINCIPALS", "{}"))
    if not isinstance(grants, dict) or any(
        not isinstance(subject, str) or not subject or not isinstance(networks, list)
        or any(not isinstance(network, str) or not network.strip() for network in networks)
        for subject, networks in grants.items()
    ):
        raise ValueError("SEARCH_NETWORK_PRINCIPALS must map authenticated subjects to network ID lists")
    return grants


def is_network_principal(user: AuthUser) -> bool:
    return user.user_id in _network_grants()


def principal_networks(user: AuthUser) -> set[str]:
    if user.token_disabled_mode:
        return set()
    return set(_network_grants().get(user.user_id, []))


def network_search_collection(user: AuthUser, network_id: str, kind: str | None, index_name: str | None) -> str:
    if index_only_mode() or network_id not in principal_networks(user):
        raise HTTPException(403, "No search access to the requested network")
    collection = managed_release_targets(kind).get(network_id)
    if not collection:
        raise HTTPException(403, "The requested network has no serving target for this kind")
    if index_name is not None and index_name != collection:
        raise HTTPException(422, "index_name does not match the authorized network target")
    return collection
