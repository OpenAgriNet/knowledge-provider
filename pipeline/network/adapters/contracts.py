"""Contract for a deployment-installed network adapter."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PublicationReceipt:
    """A correlated outcome and a transport record safe to store as an artifact."""

    exchange: dict | None = None
    message_id: str | None = None
    result_status: str | None = None
    rejection: str | None = None
    context_matches: bool = False


@dataclass(frozen=True)
class PublicationRequest:
    """Stable delivery identity and the staged snapshot for revision-based adapters."""

    network_id: str
    transaction_id: str
    document_id: str
    workflow_id: str
    kind: str
    group: str | None
    revision_id: str | None
    provider_id: str | None
    revision: dict | None


@runtime_checkable
class NetworkAdapter(Protocol):
    """Protocol-specific publication policy, identity, and delivery."""

    def validate(self) -> None: ...

    def supports_kind(self, kind: str) -> bool: ...

    def provider_id(self, kind: str, provider_code: str | None) -> str | None: ...

    def publication_evidence(self, conn, request: PublicationRequest) -> str | None: ...

    def publish_revision(self, request: PublicationRequest) -> PublicationReceipt: ...
