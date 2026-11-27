"""Resolve the deployment-owned adapter for a network connection."""

from functools import lru_cache
from importlib.metadata import entry_points

from pipeline.network.adapters.contracts import NetworkAdapter
from pipeline.network.adapters.contracts import PublicationReceipt as PublicationReceipt
from pipeline.network.network_connections import (
    NetworkConnection,
    configured_network_connections,
    network_connection,
)
from pipeline.network.network_connections import PublishConfigError as PublishConfigError

NETWORK_ADAPTER_ENTRY_POINT_GROUP = "knowledge_provider.network_adapters"


@lru_cache(maxsize=32)
def _installed_adapter_factory(name: str):
    matches = tuple(entry_points(group=NETWORK_ADAPTER_ENTRY_POINT_GROUP, name=name))
    if len(matches) != 1:
        raise PublishConfigError(
            f"Unsupported NETWORK_PUBLISHER {name!r}. Install exactly one matching network adapter."
        )
    factory = matches[0].load()
    if not callable(factory):
        raise PublishConfigError(f"Network adapter {name!r} must provide a callable factory")
    return factory


def load_network_adapter(connection: NetworkConnection) -> NetworkAdapter:
    if connection.adapter == "beckn":
        from pipeline.network.adapters.beckn import BecknNetworkPublisher

        return BecknNetworkPublisher(connection)

    adapter = _installed_adapter_factory(connection.adapter)(connection)
    if not isinstance(adapter, NetworkAdapter):
        raise PublishConfigError(f"Network adapter {connection.adapter!r} does not implement the publication contract")
    return adapter


def get_network_adapter(network_id: str | None = None) -> NetworkAdapter:
    return load_network_adapter(network_connection(network_id))


def validate_network_adapters() -> None:
    from pipeline.network.release_policy import index_only_mode

    if index_only_mode():
        return
    for connection in configured_network_connections() or (network_connection(),):
        load_network_adapter(connection).validate()


def get_network_publisher(network_id: str | None = None):
    """Resolve the catalog-only publisher used by older workflow histories."""
    connection = network_connection(network_id)
    adapter = load_network_adapter(connection)
    if connection.adapter != "beckn":
        raise PublishConfigError("Select a document revision to publish through this network adapter")
    return adapter
