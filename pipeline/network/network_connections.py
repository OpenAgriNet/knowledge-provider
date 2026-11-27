"""Deployment-owned network connections used by publishing and provider identity."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


class PublishConfigError(RuntimeError):
    """The requested network connection is missing or invalid."""


@dataclass(frozen=True)
class NetworkConnection:
    adapter: str
    network_id: str
    discovery_endpoint: str
    sender_id: str
    receiver_id: str
    visible_to: tuple[str, ...]
    registry_url: str = ""
    registry_token_file: str = ""
    release_collection: str = ""
    scheme_release_collection: str = ""
    approved_routes_file: str = ""
    options: dict[str, str] = field(default_factory=dict, repr=False)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def _validate_endpoint(endpoint: str, setting: str) -> None:
    try:
        url = urlsplit(endpoint)
        url.port
        if (url.scheme not in {"http", "https"} or not url.hostname
                or url.username is not None or url.query or url.fragment
                or any(char.isspace() or ord(char) < 32 for char in endpoint)):
            raise ValueError
    except ValueError:
        raise PublishConfigError(f"{setting} must be an HTTP(S) endpoint") from None


def _named_connections() -> dict[str, NetworkConnection]:
    raw = os.environ.get("NETWORK_CONNECTIONS")
    if raw is None:
        return {}
    names = raw.split(",")
    names = [name.strip() for name in names]
    if len(set(names)) != len(names) or any(not re.fullmatch("[a-z][a-z0-9]*", name) for name in names):
        raise PublishConfigError("NETWORK_CONNECTIONS must list distinct lowercase names")
    connections = {}
    ids = set()
    for name in names:
        prefix = f"NETWORK_CONNECTION_{name.upper()}_"

        def setting(key: str) -> str:
            return os.environ.get(prefix + key, "").strip()

        network_id = setting("ID")
        endpoint = setting("DISCOVERY_URL")
        sender_id = setting("SENDER_ID")
        adapter = setting("TYPE")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", adapter):
            raise PublishConfigError(f"{prefix}TYPE must name a network adapter")
        if not network_id or network_id in ids:
            raise PublishConfigError(f"{prefix}ID must be unique")
        registry_url = setting("REGISTRY_URL")
        if adapter == "beckn":
            if not sender_id:
                raise PublishConfigError(f"{prefix}SENDER_ID is required")
            _validate_endpoint(endpoint, prefix + "DISCOVERY_URL")
            if registry_url:
                _validate_endpoint(registry_url, prefix + "REGISTRY_URL")
        elif any(setting(key) for key in (
            "DISCOVERY_URL", "SENDER_ID", "RECEIVER_ID", "VISIBLE_TO",
            "REGISTRY_URL", "REGISTRY_TOKEN_FILE", "APPROVED_ROUTES_FILE", "SCHEME_RELEASE_COLLECTION",
        )):
            raise PublishConfigError(f"{prefix}TYPE uses adapter options instead of Beckn settings")
        option_prefix = prefix + "OPTION_"
        options = {key.removeprefix(option_prefix).lower(): value for key, value in os.environ.items()
                   if key.startswith(option_prefix)}
        ids.add(network_id)
        connections[name] = NetworkConnection(
            adapter, network_id, endpoint, sender_id, setting("RECEIVER_ID"), _csv(setting("VISIBLE_TO")),
            registry_url, setting("REGISTRY_TOKEN_FILE"),
            setting("RELEASE_COLLECTION"), setting("SCHEME_RELEASE_COLLECTION"), setting("APPROVED_ROUTES_FILE"), options,
        )
    return connections


def network_connection(network_id: str | None = None) -> NetworkConnection:
    connections = configured_network_connections()
    if connections:
        if network_id is None:
            return connections[0]
        for connection in connections:
            if connection.network_id == network_id:
                return connection
        raise PublishConfigError("The requested network has no configured connection")

    sender_id = os.environ.get("NETWORK_SENDER_ID", "").strip()
    configured_id = os.environ.get("DISCOVERY_NETWORK_ID", "").strip()
    if network_id is not None and network_id != configured_id:
        raise PublishConfigError("The requested network does not match the configured publication network")
    return NetworkConnection(
        os.environ.get("NETWORK_PUBLISHER", "beckn").strip().lower(),
        configured_id or sender_id,
        os.environ.get("DISCOVERY_SERVICE_ENDPOINT", "").strip(),
        sender_id,
        os.environ.get("DISCOVERY_RECEIVER_ID", "").strip(),
        _csv(os.environ.get("DISCOVERY_VISIBLE_TO", "")),
    )


def default_network_id() -> str:
    if "NETWORK_CONNECTIONS" in os.environ:
        return network_connection().network_id
    return os.environ.get("DISCOVERY_NETWORK_ID", "").strip()


def configured_network_connections() -> tuple[NetworkConnection, ...]:
    connections = _named_connections()
    if not connections:
        return ()
    default = os.environ.get("NETWORK_CONNECTION_DEFAULT", "").strip()
    if default not in connections:
        raise PublishConfigError("NETWORK_CONNECTION_DEFAULT must name a configured connection")
    return (connections[default], *(connection for name, connection in connections.items() if name != default))
