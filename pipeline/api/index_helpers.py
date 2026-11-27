"""Vector cleanup helpers used by document lifecycle routes."""

import logging


def delete_single_chunk_from_index(document_id: str, chunk_num: int, index_name: str | None = None) -> dict:
    """Delete a single chunk from the vector index by doc_id + chunk_num."""
    from ..vector_store import get_default_index_name, get_vector_store

    try:
        store = get_vector_store()
        resolved_index = index_name or get_default_index_name()
        return store.delete_chunk(resolved_index, document_id, chunk_num)
    except Exception as e:
        logging.exception(
            "vector chunk delete failed", extra={"doc_id": document_id, "chunk": chunk_num}
        )
        return {"deleted": False, "error": str(e)}


def delete_chunks_from_index(document_id: str, index_name: str | None = None) -> dict:
    """Delete all chunks for a document from the vector index."""
    from ..vector_store import get_default_index_name, get_vector_store

    try:
        store = get_vector_store()
        resolved_index = index_name or get_default_index_name()
        result = store.delete_by_doc_id(resolved_index, document_id)
        if "doc_id" not in result:
            result["doc_id"] = document_id
        return result
    except Exception as e:
        logging.exception("vector document delete failed", extra={"doc_id": document_id})
        return {"deleted": 0, "doc_id": document_id, "error": str(e)}
