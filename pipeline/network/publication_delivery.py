"""Persist publication receipts before proceeding to release activation."""

import re
from dataclasses import asdict, replace

from psycopg.types.json import Jsonb

from pipeline import db
from pipeline.network.adapters.contracts import PublicationReceipt, PublicationRequest
from pipeline.network.network_connections import PublishConfigError, network_connection
from pipeline.network.network_publisher import load_network_adapter

_POLICY_DIGEST = "publication_policy_sha256"


def _validate_receipt(receipt: PublicationReceipt) -> None:
    if not isinstance(receipt, PublicationReceipt) or type(receipt.context_matches) is not bool:
        raise PublishConfigError("The network adapter did not return a valid publication receipt")
    if receipt.exchange is None:
        if receipt.context_matches or receipt.result_status or receipt.rejection or receipt.message_id:
            raise PublishConfigError("The network adapter returned a receipt without its publication exchange")
    elif not isinstance(receipt.exchange, dict):
        raise PublishConfigError("The publication exchange must be an object")


def deliver_publication(
    network_id: str, transaction_id: str, document_id: str, workflow_id: str,
    document_kind: str, group: str | None, revision_id: str | None,
    *, require_staged_revision: bool = False,
) -> PublicationReceipt:
    connection = network_connection(network_id)
    adapter = load_network_adapter(connection)
    request_identity = {
        "workflow_id": workflow_id, "document_kind": document_kind,
        "group": group, "revision_id": revision_id,
    }
    with db.get_connection() as conn:
        revision = None
        provider_id = None
        if require_staged_revision or revision_id is not None or connection.adapter != "beckn":
            staged = conn.execute(
                """
                SELECT r.id::text, r.snapshot, s.provider_id
                FROM document_revisions r
                JOIN workflows w ON w.id = r.workflow_id
                JOIN network_release_staging s ON s.revision_id = r.id
                WHERE r.document_id = %s::uuid AND s.network_id = %s
                  AND ((%s::uuid IS NOT NULL AND r.id = %s::uuid)
                       OR (%s::uuid IS NULL AND w.temporal_workflow_id = %s))
                """,
                (document_id, network_id, revision_id, revision_id, revision_id, workflow_id),
            ).fetchone()
            if not staged or not staged["provider_id"]:
                raise PublishConfigError("The adapter requires a staged revision with a provider identity")
            revision_id = staged["id"]
            revision = staged["snapshot"]
            provider_id = staged["provider_id"]
            if revision.get("metadata", {}).get("kind") != document_kind or not adapter.supports_kind(document_kind):
                raise PublishConfigError("The staged revision kind is unavailable for this network adapter")
            request_identity.update(adapter=connection.adapter, revision_id=revision_id, provider_id=provider_id)

        request = PublicationRequest(
            network_id, transaction_id, document_id, workflow_id, document_kind, group,
            revision_id, provider_id, revision,
        )
        prepare_delivery = getattr(adapter, "prepare_delivery", None)
        if callable(prepare_delivery):
            policy_digest = prepare_delivery(request)
            if not isinstance(policy_digest, str) or not re.fullmatch("[0-9a-f]{64}", policy_digest):
                raise PublishConfigError("The network adapter returned an invalid publication policy digest")
            request_identity[_POLICY_DIGEST] = policy_digest
        conn.execute(
            """
            INSERT INTO publication_deliveries (network_id, transaction_id, document_id, request_identity)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (network_id, transaction_id) DO NOTHING
            """,
            (network_id, transaction_id, document_id, Jsonb(request_identity)),
        )

    # The adapter's bounded remote call runs under the delivery lock. Other
    # attempts can reuse the receipt as soon as this transaction commits.
    with db.get_connection() as conn:
        key = (network_id, transaction_id)
        delivery = conn.execute(
            """
            SELECT document_id::text, request_identity, receipt
            FROM publication_deliveries WHERE network_id = %s AND transaction_id = %s
            FOR UPDATE
            """,
            key,
        ).fetchone()
        if not delivery or delivery["document_id"] != document_id:
            raise PublishConfigError("The publication transaction already identifies a different release")
        recorded_identity = dict(delivery["request_identity"])
        current_identity = dict(request_identity)
        recorded_policy = recorded_identity.pop(_POLICY_DIGEST, None)
        current_policy = current_identity.pop(_POLICY_DIGEST, None)
        if recorded_identity != current_identity:
            raise PublishConfigError("The publication transaction already identifies a different release")
        if recorded_policy != current_policy:
            raise PublishConfigError("The network publication policy changed or was not recorded for this transaction")

        evidence = adapter.publication_evidence(conn, request)
        if delivery["receipt"] is not None:
            recorded = PublicationReceipt(**delivery["receipt"])
            _validate_receipt(recorded)
            recorded_evidence = (recorded.exchange or {}).get("binding_evidence_sha256")
            if evidence != recorded_evidence:
                raise PublishConfigError("The admission binding changed after the publication receipt")
            return recorded

        receipt = adapter.publish_revision(request)
        _validate_receipt(receipt)
        if evidence and receipt.exchange is not None:
            receipt = replace(receipt, exchange={**receipt.exchange, "binding_evidence_sha256": evidence})
        conn.execute(
            """
            UPDATE publication_deliveries SET receipt = %s, received_at = now()
            WHERE network_id = %s AND transaction_id = %s
            """,
            (Jsonb(asdict(receipt)), *key),
        )
        return receipt
