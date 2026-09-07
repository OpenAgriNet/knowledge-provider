"""
Repository over the document-related SQLite tables.

`pipeline/db.py` stays the SQL layer - one function per query, no domain
knowledge. This sits above it and answers the questions callers actually ask
("what kind of knowledge is this document?"), so an activity never has to know
that a document row is a dict, that `document_kind` is nullable with a
`'document'` default, or that recording an artifact means looking up the
latest job first.

Instantiate per use; it holds no connection of its own (`pipeline/db.py` opens
one per call).
"""

from typing import Optional

from . import db


class DocumentRepository:
    """Reads and writes the `documents` row and its artifacts."""

    def __init__(self, db_module=None):
        # Injectable so tests can hand in a fake without patching the module
        # globally; defaults to the real SQLite layer.
        self._db = db_module or db

    def get_document_kind(self, workflow_id: str) -> Optional[str]:
        """The document's knowledge kind, or None if unset or unknown.

        Returns the column verbatim - normalisation belongs to the caller that
        knows what it means (see `network_catalog.normalize_document_kind`).
        A missing document and an unclassified one are both None: neither has
        a kind, and no caller so far needs to tell them apart.
        """
        doc = self._db.get_document(workflow_id)
        if not doc:
            return None
        return doc.get("document_kind")

    def get_latest_job_id(self, workflow_id: str) -> Optional[int]:
        """Id of the most recent job for this document, or None if it has none."""
        job = self._db.get_latest_document_job(workflow_id)
        return job["id"] if job else None

    def record_artifact(
        self,
        *,
        workflow_id: str,
        artifact_type: str,
        stage: str,
        storage_uri: str,
        mime_type: Optional[str] = None,
        filename: Optional[str] = None,
        size_bytes: Optional[int] = None,
        metadata: Optional[dict] = None,
    ) -> int:
        """Attach an artifact to the document's latest job.

        The job lookup is done here rather than by the caller: every artifact
        written during the pipeline belongs to the run that produced it, and
        callers were all repeating the same lookup-then-guard.
        """
        return self._db.add_document_artifact(
            workflow_id=workflow_id,
            job_id=self.get_latest_job_id(workflow_id),
            artifact_type=artifact_type,
            stage=stage,
            storage_uri=storage_uri,
            mime_type=mime_type,
            filename=filename,
            size_bytes=size_bytes,
            metadata=metadata,
        )
