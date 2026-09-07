"""
Client for the external Discovery Service's Beckn-style catalog/publish endpoint.

Kept separate from pipeline/activities.py so the outbound call and envelope
shape are testable without Temporal, and reusable outside the pipeline activity.
Deliberately Temporal-free: the activity is what translates a rejection into a
Temporal failure.

What travels on the wire is decided by pipeline/network_catalog.py; this module
owns the environment and the transport only.
"""

import logging
import os
import uuid
from datetime import datetime
from typing import Optional

import httpx

from .network_catalog import build_catalog, normalize_document_kind

logger = logging.getLogger(__name__)

BECKN_VERSION = "2.0.0"
DEFAULT_NETWORK_ID = "da.gov.in/vistaar"


class DiscoveryPublishService:
    """POSTs a Beckn-shaped catalog/publish envelope to the Discovery Service."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        sender_id: Optional[str] = None,
        sender_uri: Optional[str] = None,
        network_id: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.endpoint = endpoint or os.environ.get("DISCOVERY_SERVICE_ENDPOINT")
        self.sender_id = sender_id or os.environ.get("NETWORK_SENDER_ID")
        self.sender_uri = sender_uri or os.environ.get("NETWORK_SENDER_URI")
        # Optional: the receiver falls back to its own default when absent, so
        # an unset NETWORK_ID must not stop the pipeline.
        self.network_id = network_id or os.environ.get("NETWORK_ID") or DEFAULT_NETWORK_ID

        missing = [
            name
            for name, value in (
                ("DISCOVERY_SERVICE_ENDPOINT", self.endpoint),
                ("NETWORK_SENDER_ID", self.sender_id),
                ("NETWORK_SENDER_URI", self.sender_uri),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"{', '.join(missing)} must be set to publish to the network."
            )

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
        kind = normalize_document_kind(document_kind)
        catalog = build_catalog(kind, bpp_id=self.sender_id, bpp_uri=self.sender_uri)
        if catalog is None:
            logger.info(
                "workflow_id=%s document_kind=%s network_publish_skipped=True",
                workflow_id,
                kind,
            )
            return {
                "skipped": True,
                "document_kind": kind,
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
                "networkId": self.network_id,
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
            "document_kind": kind,
            "envelope": envelope,
            "status_code": response.status_code,
            "response_body": response.text,
        }
