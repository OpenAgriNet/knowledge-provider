"""Check a managed publication against locally approved Registry bindings."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.network.catalog.catalog_profile import CatalogProjection
from pipeline.network.network_connections import PublishConfigError, configured_network_connections
from pipeline.network.network_group import load_provider_naming, named_group, provider_for
from pipeline.network.network_registry import (
    ApprovedRoute,
    RegistryLookupError,
    registry_for_network,
)

_MAX_APPROVAL_FILE_BYTES = 256 * 1024


def publication_admission_evidence(
    network_id: str, document_kind: str, group: str | None, projection: CatalogProjection | None,
    *, participant_id: str | None = None,
) -> str | None:
    connection = next((item for item in configured_network_connections() if item.network_id == network_id), None)
    if not connection or not connection.approved_routes_file or not projection:
        return None
    if not projection.schema_pack:
        raise PublishConfigError("Registry admission requires a pinned catalog schema pack")
    if projection.resource_attributes.get("informationMode") != "OnDemand":
        raise PublishConfigError("The approved publication route requires an OnDemand catalog")
    if not participant_id:
        try:
            provider = provider_for(load_provider_naming(network_id), document_kind, named_group(group))
        except ValueError as exc:
            raise PublishConfigError("The catalog provider identity is invalid for Registry admission") from exc
        if not provider:
            raise PublishConfigError("The catalog has no provider identity for Registry admission")
        participant_id = provider["id"]

    try:
        with Path(connection.approved_routes_file).open("rb") as handle:
            raw = handle.read(_MAX_APPROVAL_FILE_BYTES + 1)
        if len(raw) > _MAX_APPROVAL_FILE_BYTES:
            raise ValueError("The approval file exceeds its size limit")
        rows = json.loads(raw)
        if not isinstance(rows, list):
            raise ValueError("The approval file must contain a list")
        approvals = [
            ApprovedRoute(**row) for row in rows
            if isinstance(row, dict)
            and (row.get("network_id"), row.get("participant_id"), row.get("capability"), row.get("action"))
            == (network_id, participant_id, projection.schema_pack.capability, "select")
        ]
    except (OSError, TypeError, ValueError):
        raise PublishConfigError("The configured Registry route approval file is invalid") from None
    if len(approvals) != 1:
        raise PublishConfigError("The network requires exactly one approved provider select route")

    try:
        return registry_for_network(network_id).ready_route(approvals[0]).digest
    except RegistryLookupError as exc:
        if exc.reason == "unavailable":
            raise RuntimeError("The configured Registry is temporarily unavailable") from exc
        raise PublishConfigError("The approved provider route is not ready in the Registry") from exc
    except ValueError as exc:
        raise PublishConfigError("The network Registry configuration is invalid") from exc
