"""
Repository over the `documents` table.

pipeline/db.py stays the SQL layer - one function per query, no domain
knowledge. This sits above it and answers the question the caller actually has,
so a caller never unpacks a raw document row.
"""

from typing import Optional

from . import db
from .document_validity import ValidityPeriod, period_from_row


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

    def get_validity(self, workflow_id: str) -> Optional[ValidityPeriod]:
        """The period this document's chunks are searchable in, or None when
        it has none.

        None is not an error: documents uploaded before validity existed have
        no stored dates, and their chunks are searchable without restriction
        (see `document_validity.period_from_row`).
        """
        doc = self._db.get_document(workflow_id)
        if not doc:
            return None
        return period_from_row(doc.get("valid_from"), doc.get("valid_to"))
