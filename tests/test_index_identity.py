"""Pure checks for document-owned projection keys; no application configuration."""

import unittest

from pipeline.search.index_identity import projection_doc_id, projection_owner, recorded_doc_id


class TestIndexIdentity(unittest.TestCase):
    def setUp(self):
        self.document = {
            "id": "00000000-0000-0000-0000-000000000001",
            "document_id": "a" * 32,
        }

    def test_identical_files_have_independent_document_keys(self):
        other = {**self.document, "id": "00000000-0000-0000-0000-000000000002"}
        self.assertNotEqual(projection_doc_id(self.document), projection_doc_id(other))

    def test_retries_keep_the_key_and_replacements_change_it(self):
        key = projection_doc_id(self.document)
        self.assertEqual(key, projection_doc_id({**self.document, "workflow_id": "new-attempt"}))
        self.assertNotEqual(key, projection_doc_id(self.document, "b" * 32))

    def test_source_identity_remains_unchanged(self):
        projection_doc_id(self.document)
        self.assertEqual(self.document["document_id"], "a" * 32)

    def test_scoped_citation_resolves_to_its_owner(self):
        self.assertEqual(projection_owner(projection_doc_id(self.document)), self.document["id"])
        for identifier in ("a" * 32, "document:invalid:source", "document::"):
            self.assertIsNone(projection_owner(identifier))

    def test_inspection_and_deletion_use_the_recorded_network_key(self):
        document = {**self.document, "index_status": {
            "dev": {"doc_id": projection_doc_id(self.document)},
            "prod": {"doc_id": "old-source-fingerprint"},
        }}
        self.assertEqual(recorded_doc_id(document, "dev"), projection_doc_id(self.document))
        self.assertEqual(recorded_doc_id(document, "prod"), "old-source-fingerprint")
        self.assertEqual(recorded_doc_id(self.document, "dev"), "a" * 32)


if __name__ == "__main__":
    unittest.main()
