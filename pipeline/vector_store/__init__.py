"""Vector index backend — Qdrant."""

from __future__ import annotations

import os

from .base import VectorStore


def get_default_index_name() -> str:
    return os.environ.get("VECTOR_DB_COLLECTION_NAME") or "documents-index"


def get_vector_store() -> VectorStore:
    from .qdrant_store import QdrantVectorStore

    return QdrantVectorStore()


__all__ = [
    "VectorStore",
    "get_default_index_name",
    "get_vector_store",
]
