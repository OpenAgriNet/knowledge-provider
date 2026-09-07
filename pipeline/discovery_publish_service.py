"""
Client for the external Discovery Service's Beckn-style catalog/publish endpoint.

Kept separate from pipeline/activities.py so the outbound call and envelope
shape are testable without Temporal, and reusable outside the pipeline activity.

pipeline/catalog_builder.py decides what travels on the wire; this module owns
the environment and the transport.
"""

import logging
import os
import uuid
from datetime import datetime
from typing import Optional

import httpx

from .catalog_builder import build_catalog
from .network_constants import BECKN_VERSION

logger = logging.getLogger(__name__)


class DiscoveryPublishService:
    """POSTs a Beckn-shaped catalog/publish envelope to the Discovery Service."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        sender_id: Optional[str] = None,
        sender_uri: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.endpoint = endpoint or os.environ.get("DISCOVERY_SERVICE_ENDPOINT")
        # Doubles as context.networkId.
        self.sender_id = sender_id or os.environ.get("NETWORK_SENDER_ID")
        self.sender_uri = sender_uri or os.environ.get("NETWORK_SENDER_URI")

        if not self.endpoint:
            raise RuntimeError("DISCOVERY_SERVICE_ENDPOINT must be set to publish to the network.")
        if not self.sender_id:
            raise RuntimeError("NETWORK_SENDER_ID must be set to publish to the network.")
        if not self.sender_uri:
            raise RuntimeError("NETWORK_SENDER_URI must be set to publish to the network.")

        self.timeout = timeout

    def publish(
        self,
        transaction_id: str,
        document_kind: Optional[str],
        workflow_id: Optional[str] = None,
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
        catalog = build_catalog(document_kind, bpp_id=self.sender_id, bpp_uri=self.sender_uri)
        if catalog is None:
            logger.info(
                "workflow_id=%s document_kind=%s network_publish_skipped=True",
                workflow_id,
                document_kind,
            )
            return {
                "skipped": True,
                "envelope": None,
                "status_code": None,
                "response_body": None,
            }

        envelope = {
            "context": {
                "action": "catalog/publish",
                "version": BECKN_VERSION,
                "messageId": str(uuid.uuid4()),
                "transactionId": transaction_id,
                "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
                "senderId": self.sender_id,
                "networkId": self.sender_id,
            },
            "message": {
                "catalogs": [catalog],
                # MERGE is the documented default, but FULL deletes whatever a
                # payload omits - too sharp a difference to leave implicit.
                "publishDirectives": [
                    {
                        "catalogId": catalog["id"],
                        "catalogType": "REGULAR",
                        "updateMode": "MERGE",
                    }
                ],
            },
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.endpoint.rstrip('/')}/publish", json=envelope)
            response.raise_for_status()

        return {
            "skipped": False,
            "envelope": envelope,
            "status_code": response.status_code,
            "response_body": response.text,
        }
