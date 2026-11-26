"""
Client for the external Discovery Service's Beckn-style catalog/publish endpoint.

Kept separate from pipeline/activities.py so the outbound call and envelope
shape are testable without Temporal, and reusable outside the pipeline activity.

pipeline/catalog_builder.py decides what travels on the wire; this module owns
the environment and the transport.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx

from pipeline.network.catalog.catalog_builder import build_catalog, normalize_document_kind
from pipeline.network.catalog.catalog_profile import CatalogProjection, load_catalog_profile
from pipeline.network.network_connections import (
    NetworkConnection,
    PublishConfigError,
    network_connection,
)
from pipeline.network.network_constants import BECKN_VERSION, RESULT_ACCEPTED
from pipeline.network.network_group import load_provider_naming, named_group, provider_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedCatalog:
    projection: CatalogProjection
    context: dict
    catalog: dict
    directive: dict


class DiscoveryPublishService:
    """POSTs a Beckn-shaped catalog/publish envelope to the Discovery Service."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        timeout: float = 30.0,
        *,
        connection: NetworkConnection | None = None,
    ):
        connection = connection or network_connection()
        self.endpoint = endpoint or connection.discovery_endpoint
        self.sender_id = connection.sender_id
        self.network_id = connection.network_id
        self.receiver_id = connection.receiver_id
        self.visible_to = connection.visible_to
        self.provider_naming = load_provider_naming(self.network_id)

        if not self.endpoint:
            raise PublishConfigError("DISCOVERY_SERVICE_ENDPOINT must be set to publish to the network.")
        if not self.sender_id:
            raise PublishConfigError("NETWORK_SENDER_ID must be set to publish to the network.")

        self.timeout = timeout

    def publish(
        self,
        transaction_id: str,
        document_kind: Optional[str],
        workflow_id: Optional[str] = None,
        group: Optional[str] = None,
    ) -> dict:
        """
        POST a catalog/publish envelope for `document_kind`.

        `transaction_id` is supplied by the caller (generated once per publish,
        reused across retries) - only `messageId` is generated fresh here, per
        Beckn convention.

        A kind with no catalog mapped to it makes no HTTP call and comes back
        with `skipped=True`: the spec declares `message.catalogs` as
        `minItems: 1`, so there is no valid "publish nothing" request to send.
        """
        prepared = self.prepare(document_kind, group)
        if prepared is None:
            logger.info(
                "network publish skipped",
                extra={"workflow_id": workflow_id, "document_kind": document_kind},
            )
            return self._skipped()
        return self.publish_prepared(transaction_id, prepared, workflow_id)

    def prepare(self, document_kind: str | None, group: str | None) -> PreparedCatalog | None:
        kind = normalize_document_kind(document_kind)
        projection = load_catalog_profile().get(kind)
        if projection is None:
            return None
        catalog_group = named_group(group)
        provider = provider_for(self.provider_naming, document_kind, catalog_group)
        catalog_name = self.provider_naming.catalog_name(kind, catalog_group)
        catalog = build_catalog(document_kind, provider, catalog_name, catalog_group, spec=projection)

        context = {
            "domain": projection.domain,
            "action": "catalog/publish",
            "version": BECKN_VERSION,
            "senderId": self.sender_id,
            "networkId": self.network_id,
        }
        if self.receiver_id:
            context["receiverId"] = self.receiver_id

        directive = {
            "catalogId": catalog["id"],
            "catalogType": "REGULAR",
            "updateMode": "MERGE",
        }
        if self.visible_to:
            directive["visibleTo"] = list(self.visible_to)
        return PreparedCatalog(projection, context, catalog, directive)

    def publish_prepared(
        self, transaction_id: str, prepared: PreparedCatalog, workflow_id: str | None = None,
    ) -> dict:
        context = {
            **prepared.context,
            "messageId": str(uuid.uuid4()),
            "transactionId": transaction_id,
            "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
        }
        catalog = prepared.catalog
        envelope = {
            "context": context,
            "message": {"catalogs": [catalog], "publishDirectives": [prepared.directive]},
        }
        url = f"{self.endpoint.rstrip('/')}/publish"
        publish_fields = {
            "workflow_id": workflow_id,
            "catalog_id": catalog["id"],
            "transaction_id": transaction_id,
        }
        logger.info(
            "network publish sent",
            extra={
                **publish_fields,
                "message_id": envelope["context"]["messageId"],
                "network_publish_url": url,
            },
        )
        logger.debug("Request to %s with \n body %s", url, envelope)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(url, json=envelope)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # The activity records no artifact when publish raises, so this is
            # the only trace of a transport failure.
            logger.error(
                "network publish failed",
                extra={**publish_fields, "error": str(exc), "response_body": exc.response.text},
            )
            raise
        except httpx.HTTPError as exc:
            logger.error("network publish failed", extra={**publish_fields, "error": str(exc)})
            raise

        logger.info(
            "network publish answered",
            extra={**publish_fields, "network_publish_status": response.status_code},
        )

        result_status, errors = self._read_result(response, catalog["id"], workflow_id)
        try:
            reply_context = response.json()["context"]
            context_matches = isinstance(reply_context, dict) and all(
                reply_context.get(key) == expected for key, expected in {
                    "action": "catalog/on_publish",
                    "transactionId": context["transactionId"],
                    "networkId": context["networkId"],
                    "receiverId": context["senderId"],
                    **({"senderId": context["receiverId"]} if context.get("receiverId") else {}),
                }.items()
            )
        except (KeyError, TypeError, ValueError):
            context_matches = False
        return {
            "skipped": False,
            "envelope": envelope,
            "status_code": response.status_code,
            "response_body": response.text,
            "result_status": result_status,
            "context_matches": context_matches,
            "errors": errors,
        }

    @staticmethod
    def _skipped() -> dict:
        return {
            "skipped": True,
            "envelope": None,
            "status_code": None,
            "response_body": None,
            "result_status": None,
            "errors": [],
        }

    def _read_result(
        self,
        response: httpx.Response,
        catalog_id: str,
        workflow_id: Optional[str],
    ) -> tuple[Optional[str], list]:
        """This catalog's result status and errors from an on_publish body.

        Deliberately lenient: an unreadable or absent `message.results` reads as
        an unknown status, never a failure - a peer that deviates from the spec
        should not stall the pipeline. Only an explicit REJECTED is a rejection,
        and the caller decides what that costs.
        """
        try:
            results = response.json()["message"]["results"]
            if not isinstance(results, list) or not results:
                raise ValueError("no results")
        except Exception:
            logger.warning(
                "network publish result unreadable",
                extra={
                    "workflow_id": workflow_id,
                    "catalog_id": catalog_id,
                    "response_body": response.text[:200],
                },
            )
            return None, []

        result = next(
            (r for r in results if isinstance(r, dict) and r.get("catalogId") == catalog_id),
            {},
        )
        result_status = result.get("status")
        errors = result.get("errors") or []

        if result_status != RESULT_ACCEPTED:
            logger.warning(
                "network publish not accepted",
                extra={
                    "workflow_id": workflow_id,
                    "catalog_id": catalog_id,
                    "network_publish_result": result_status,
                    "errors": str(errors),
                },
            )

        return result_status, errors
