"""Beckn catalog publication and Registry admission."""

import hashlib
import json

from pipeline.auth.tenancy import group_for_instance
from pipeline.metadata.schema_packs import get_schema_pack
from pipeline.network.adapters.contracts import PublicationReceipt, PublicationRequest
from pipeline.network.catalog.catalog_builder import is_publishable_kind
from pipeline.network.catalog.catalog_profile import load_catalog_profile
from pipeline.network.catalog.discovery_publish_service import (
    DiscoveryPublishService,
    PreparedCatalog,
)
from pipeline.network.network_connections import NetworkConnection, PublishConfigError
from pipeline.network.network_constants import RESULT_REJECTED
from pipeline.network.network_group import load_provider_naming, named_group, provider_for
from pipeline.network.network_provider import InvalidProviderIdError
from pipeline.network.publication_admission import publication_admission_evidence


class BecknNetworkPublisher:
    def __init__(self, connection: NetworkConnection) -> None:
        self.connection = connection
        self._prepared: tuple[DiscoveryPublishService, PreparedCatalog | None] | None = None

    def validate(self) -> None:
        load_provider_naming(self.connection.network_id)

    def supports_kind(self, kind: str) -> bool:
        return is_publishable_kind(kind)

    def provider_id(self, kind: str, provider_code: str | None) -> str | None:
        naming = load_provider_naming(self.connection.network_id)
        provider = provider_for(naming, kind, named_group(group_for_instance(provider_code)))
        return provider["id"] if provider else None

    def prepare_delivery(self, request: PublicationRequest) -> str:
        try:
            service = DiscoveryPublishService(connection=self.connection)
            prepared = service.prepare(request.kind, request.group)
        except ValueError as exc:
            raise PublishConfigError("The Beckn publication policy is invalid") from exc
        provider = (prepared.catalog.get("provider") or {}) if prepared else {}
        if request.provider_id and provider.get("id") != request.provider_id:
            raise PublishConfigError("The Beckn catalog provider no longer matches the staged release")
        self._prepared = (service, prepared)
        policy = {
            "endpoint": service.endpoint.rstrip("/"),
            "context": prepared.context if prepared else None,
            "catalog": prepared.catalog if prepared else None,
            "directive": prepared.directive if prepared else None,
            "schema_pin": (
                prepared.projection.schema_pack.model_dump()
                if prepared and prepared.projection.schema_pack else None
            ),
        }
        return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def publication_evidence(self, conn, request: PublicationRequest) -> str | None:
        prepared = self._prepared[1] if self._prepared else None
        projection = prepared.projection if prepared else None
        if self._prepared is None:
            projection = load_catalog_profile().get(request.kind)
        if projection and projection.schema_pack:
            try:
                pin = projection.schema_pack
                declared = projection.resource_attributes.get("@type")
                declared_types = declared if isinstance(declared, list) else [declared]
                if pin.capability not in declared_types:
                    raise ValueError("The pinned capability does not match the catalog resource type")
                pack = get_schema_pack(conn, request.network_id, pin.capability, pin.digest)
                pack.validate_attributes(projection.resource_attributes)
            except ValueError as exc:
                raise PublishConfigError("The network catalog does not match its pinned schema pack") from exc
        participant = (prepared.catalog.get("provider") or {}).get("id") if prepared else None
        return publication_admission_evidence(
            request.network_id, request.kind, request.group, projection, participant_id=participant,
        )

    def publish_revision(self, request: PublicationRequest) -> PublicationReceipt:
        return self.publish(request.transaction_id, request.kind, request.workflow_id, request.group)

    def publish(self, transaction_id: str, document_kind: str, workflow_id: str, group: str | None) -> PublicationReceipt:
        try:
            if self._prepared:
                service, prepared = self._prepared
                if prepared is None:
                    return PublicationReceipt()
                result = service.publish_prepared(transaction_id, prepared, workflow_id)
            else:
                result = DiscoveryPublishService(connection=self.connection).publish(
                    transaction_id, document_kind, workflow_id, group,
                )
        except InvalidProviderIdError as exc:
            raise PublishConfigError(str(exc)) from exc

        if result["skipped"]:
            return PublicationReceipt()

        status = result["result_status"]
        rejection = None
        if status == RESULT_REJECTED:
            rejection = f"Discovery Service rejected catalog for {document_kind}: {result['errors']}"
        return PublicationReceipt(
            exchange={
                "request": result["envelope"],
                "response_status": result["status_code"],
                "response_body": result["response_body"],
                "result_status": status,
                "context_matches": result.get("context_matches", False),
                "errors": result["errors"],
            },
            message_id=result["envelope"]["context"]["messageId"],
            result_status=status,
            rejection=rejection,
            context_matches=result.get("context_matches", False),
        )
