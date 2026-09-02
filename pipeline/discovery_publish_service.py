"""
Client for the external Discovery Service's Beckn-style catalog/publish endpoint.

Kept separate from pipeline/activities.py so the outbound call and envelope
shape are testable without Temporal, and reusable outside the pipeline activity.
"""

import os
import uuid
from datetime import datetime
from typing import Optional

import httpx


class DiscoveryPublishService:
    """POSTs a Beckn-shaped catalog/publish envelope to the Discovery Service."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        sender_id: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.endpoint = endpoint or os.environ.get("DISCOVERY_SERVICE_ENDPOINT")
        self.sender_id = sender_id or os.environ.get("NETWORK_SENDER_ID")
        if not self.endpoint or not self.sender_id:
            raise RuntimeError(
                "DISCOVERY_SERVICE_ENDPOINT and NETWORK_SENDER_ID must both be set "
                "to publish to the network."
            )
        self.timeout = timeout

    def publish(self, transaction_id: str) -> dict:
        """
        POST a catalog/publish envelope. `transaction_id` is supplied by the
        caller (generated once per publish, reused across retries) - only
        `messageId` is generated fresh here, per Beckn convention.
        """
        envelope = {
            "context": {
                "action": "catalog/publish",
                "messageId": str(uuid.uuid4()),
                "transactionId": transaction_id,
                "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
                "senderId": self.sender_id,
            },
            "message": {"catalogs": []},
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.endpoint.rstrip('/')}/publish", json=envelope)
            response.raise_for_status()

        return {
            "envelope": envelope,
            "status_code": response.status_code,
            "response_body": response.text,
        }
