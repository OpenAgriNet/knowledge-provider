"""
Repository over the `documents` table.

pipeline/db.py stays the SQL layer - one function per query, no domain
knowledge. This sits above it and answers the question the caller actually has,
so a caller never unpacks a raw document row.
"""

from typing import Optional

from . import db
from .network_validity import ValidityWindow, window_from_row


class DocumentRepository:
    """Reads the `documents` row."""

    def __init__(self, db_module=None):
        # Injectable so tests can hand in a fake; defaults to the real layer.
        self._db = db_module or db

    def get_document_kind(self, workflow_id: str) -> Optional[str]:
        """The document's knowledge kind, or None if unset or unknown.

        Returned verbatim - normalisation belongs to the caller that knows what
        the kind means (see `catalog_builder.normalize_document_kind`).
        """
        doc = self._db.get_document(workflow_id)
        if not doc:
            return None
        return doc.get("document_kind")

    def get_network_validity(self, workflow_id: str) -> Optional[ValidityWindow]:
        """The lifetime the prod approver set for this document's network
        announcement, or None when it has none.

        None is not an error: documents promoted before approvers named a
        window have no stored dates, and those publish without a validity.
        """
        doc = self._db.get_document(workflow_id)
        if not doc:
            return None
        return window_from_row(
            doc.get("network_valid_from"), doc.get("network_valid_to")
        )
