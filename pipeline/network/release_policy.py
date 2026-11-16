"""Provider display and network release settings."""

from __future__ import annotations

import json
import os

from pipeline.network.network_connections import (
    PublishConfigError,
    configured_network_connections,
    network_connection,
)


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"true", "1", "yes"}


def index_only_mode() -> bool:
    """Finish processing at the local search collection without releasing documents."""
    return _enabled("KP_INDEX_ONLY")


def intake_index_options(index_name: str, auto_approve: bool) -> tuple[str, bool]:
    from pipeline.vector_store import get_default_index_name

    return (get_default_index_name(), True) if index_only_mode() else (index_name, auto_approve)


def publish_to_network_enabled() -> bool:
    return not index_only_mode() and _enabled("ENABLE_PUBLISH_TO_NETWORK")


def _configured_upload_release_target(kind: str | None) -> tuple[str, str] | None:
    """Return the deployment's automatic upload network and local collection."""
    if index_only_mode():
        return None
    network_id = os.environ.get("AUTO_RELEASE_NETWORK_ID", "").strip()
    if not network_id:
        return None
    try:
        connection = network_connection(network_id)
    except PublishConfigError as exc:
        raise ValueError(str(exc)) from exc
    if not publish_to_network_enabled() or network_id != network_connection().network_id:
        raise ValueError("Automatic upload release must target the enabled local network")
    collection = os.environ.get("AUTO_RELEASE_COLLECTION", "").strip() or connection.release_collection
    if kind == "scheme" and connection.adapter == "beckn":
        collection = (
            os.environ.get("AUTO_RELEASE_SCHEME_COLLECTION", "").strip()
            or connection.scheme_release_collection
            or collection
        )
    if not collection:
        raise ValueError("AUTO_RELEASE_COLLECTION is required for automatic upload release")
    draft_collection = os.environ.get("VECTOR_DB_COLLECTION_NAME", "documents-index").strip() or "documents-index"
    if collection == draft_collection or collection == "documents-index":
        raise ValueError("A network release collection must be separate from the draft index")
    return network_id, collection


def upload_release_target(kind: str | None = None, *, provider: str | None = None) -> tuple[str, str] | None:
    from pipeline.network.network_publisher import get_network_adapter

    if provider and kind and publish_to_network_enabled():
        from pipeline.intake.provider_sources import stored_settings

        row = stored_settings(provider)
        network_id = (row or {}).get("settings", {}).get("release_network_id")
        if network_id:
            collection = managed_release_targets(kind).get(network_id)
            if not collection:
                raise ValueError("The provider’s default release network cannot publish this document kind")
            return network_id, collection
    target = _configured_upload_release_target(kind)
    if target and kind:
        try:
            if not get_network_adapter(target[0]).supports_kind(kind):
                return None
        except PublishConfigError as exc:
            raise ValueError(str(exc)) from exc
    return target


def managed_release_targets(kind: str | None = None) -> dict[str, str]:
    """Map explicit local write targets to their independently managed collections."""
    return _managed_release_targets(kind, require_support=True)


def source_release_target(kind: str, network_id: str | None, *, provider: str | None = None) -> tuple[str, str] | None:
    if network_id is None:
        return upload_release_target(kind, provider=provider)
    collection = managed_release_targets(kind).get(network_id)
    return (network_id, collection) if collection else None


def _managed_release_targets(kind: str | None, *, require_support: bool) -> dict[str, str]:
    from pipeline.network.network_publisher import load_network_adapter

    automatic = upload_release_target(kind) if require_support else _configured_upload_release_target(kind)
    if not publish_to_network_enabled():
        return {}
    try:
        connections = configured_network_connections()
    except PublishConfigError as exc:
        raise ValueError(str(exc)) from exc
    default_collection = os.environ.get("VECTOR_DB_COLLECTION_NAME", "documents-index").strip()
    collections: dict[str, str] = {}
    targets = {}
    for connection in connections:
        general = connection.release_collection
        schemes = connection.scheme_release_collection or general
        for collection in (general, schemes):
            if not collection:
                continue
            if collection in {default_collection, "documents-index"}:
                raise ValueError("A network release collection must be separate from the draft index")
            owner = collections.setdefault(collection, connection.network_id)
            if owner != connection.network_id:
                raise ValueError("Each network must have a separate release collection")
        collection = schemes if kind == "scheme" else general
        try:
            supported = not (require_support and kind) or load_network_adapter(connection).supports_kind(kind)
        except PublishConfigError as exc:
            raise ValueError(str(exc)) from exc
        if collection and supported:
            targets[connection.network_id] = collection
    if automatic:
        network_id, collection = automatic
        if collections.get(collection, network_id) != network_id:
            raise ValueError("The automatic release collection belongs to a different network")
        if targets.get(network_id, collection) != collection:
            raise ValueError("The automatic and managed release collections must agree")
        targets[network_id] = collection
    return targets


def managed_release_collections() -> dict[str, str]:
    """Map collection to its publication authority for search filtering."""
    collections = {}
    for kind in (None, "scheme"):
        for network_id, collection in _managed_release_targets(kind, require_support=False).items():
            owner = collections.setdefault(collection, network_id)
            if owner != network_id:
                raise ValueError("Each network must have a separate release collection")
    return collections


def prod_stage_disabled() -> bool:
    """True when DISABLE_PROD_SETTING turns off the PROD half of the pipeline.

    With this set, a document finishes at DEV ingest instead of waiting for a
    superadmin to promote it — the ``approval_for_prod`` and ``ingesting_prod``
    stages are skipped entirely.

    Read only when a workflow is *started*; the value is then passed into the
    workflow as an argument. Reading it inside a workflow would break Temporal
    replay, because flipping the flag mid-flight would send a replaying
    workflow down a different branch than the one recorded in its history.
    """
    return index_only_mode() or _enabled("DISABLE_PROD_SETTING")

def instance_display_name(code: str | None) -> str:
    """Resolve a deployment's provider label or display the code itself."""
    normalized = (code or "").strip().lower()
    configured = json.loads(os.environ.get("PROVIDER_DISPLAY_NAMES", "{}"))
    if not isinstance(configured, dict) or any(
        not isinstance(name, str) or not name.strip() for name in configured.values()
    ):
        raise ValueError("PROVIDER_DISPLAY_NAMES must map provider codes to display names")
    return configured.get(normalized) or normalized.replace("-", " ").replace("_", " ").title() or "Default"
