"""Unit tests for the Group field on Qdrant chunk payloads (ADR 0006)."""

import pytest
from qdrant_client.http import models as qmodels

from pipeline.vector_store.qdrant_store import (
    PAYLOAD_FIELDS,
    QdrantVectorStore,
    _record_payload,
)


class TestGroupPayload:
    @pytest.mark.unit
    def test_carries_the_group_beside_the_instance(self):
        payload = _record_payload(
            {"text": "x", "instance": "mh", "instance_name": "Maharashtra", "group": "mh"}
        )

        assert payload["group"] == "mh"
        assert payload["instance"] == "mh"
        assert payload["instance_name"] == "Maharashtra"

    @pytest.mark.unit
    def test_a_chunk_with_no_group_has_no_group_field(self):
        # Absent, not null: Bharat Vistaar and 'default' chunks have no Group.
        payload = _record_payload({"text": "x", "instance": "bv", "group": None})

        assert "group" not in payload
        assert payload["instance"] == "bv"

    @pytest.mark.unit
    def test_group_is_a_declared_payload_field(self):
        assert "group" in PAYLOAD_FIELDS

    @pytest.mark.unit
    def test_group_is_indexed_as_a_keyword_like_instance(self):
        assert QdrantVectorStore.PAYLOAD_INDEXES["group"] == qmodels.PayloadSchemaType.KEYWORD
