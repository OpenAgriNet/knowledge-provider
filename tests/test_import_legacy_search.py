from types import SimpleNamespace

import pytest

from pipeline import db
from tests.migration_kit import load_migration_tool


class LegacyCollection:
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint

    def scroll(self, _collection, **_):
        return [SimpleNamespace(payload={
            "workflow_id": "legacy-public-document", "doc_id": self.fingerprint,
            "chunk_num": 1, "text": "Previously searchable passage.",
            "doc_language": "gu",
        })], None


@pytest.mark.db
def test_import_uses_published_text_without_reopening_review(db_connection):
    import_legacy_search = load_migration_tool("import_legacy_search").import_legacy_search
    project = load_migration_tool("rebuild_qdrant_from_postgres")._projection
    fingerprint = "a" * 32
    document_id = db.upsert_document(
        workflow_id="legacy-public-document", document_id=fingerprint, filename="legacy.pdf",
        filepath="minio://docs/legacy.pdf", instance="bv", kind="advisory", code="legacy",
    )
    db.update_document_stage("legacy-public-document", "approval_for_prod")
    assert import_legacy_search(LegacyCollection(fingerprint), "published")["passages"] == 1
    assert db.list_document_revisions(document_id)["revisions"] == []
    for _ in range(2):
        import_legacy_search(LegacyCollection(fingerprint), "published", apply=True)
    with pytest.raises(ValueError, match="source version"):
        import_legacy_search(LegacyCollection("b" * 32), "published", apply=True)

    document = db.get_document(document_id)
    assert document["stage"] == "approval_for_prod"
    assert document["workflow_id"] == "legacy-public-document"
    revisions = db.list_document_revisions(document_id)
    assert len(revisions["revisions"]) == 1
    assert revisions["releases"] == []
    records = project(document, private_rehearsal=False)
    assert records[0]["text"] == "Previously searchable passage."
    assert records[0]["instance"] == "bv"
    assert "primary_language" not in records[0]
