"""Qdrant vector store for docs-pipeline passage chunks."""

from __future__ import annotations

import logging
import math
import os
import re
from typing import Any, Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from qdrant_client.http.exceptions import UnexpectedResponse

from pipeline.documents.document_validity import Clock, system_clock, today
from pipeline.metadata.localization import indexed_document_labels, normalize_language_tag
from pipeline.search.document_identity import indexed_identity

from .embeddings import embed_passages, embed_query, get_embedding_prompt_style, get_vector_size

logger = logging.getLogger(__name__)

_client_cache: dict[tuple[str, Optional[str]], QdrantClient] = {}

PAYLOAD_FIELDS = (
    "doc_id",
    "workflow_id",
    "instance",
    "provider_id",
    "localized_labels",
    "topics",
    "document_tags",
    "subject_categories",
    "metadata",
    "crop_keys",
    "document_scope",
    "subjects",
    "agriculture_subject_ids",
    "coverage_area_keys",
    "excluded_coverage_area_keys",
    "season_keys",
    "applicability_groups",
    "applicability_scope",
    "tag_keys",
    "chunk_tags",
    "source_url",
    "type",
    "source",
    "filename",
    "name",
    "primary_language",
    "primary_text",
    "text_language",
    "translations",
    "source_evidence",
    "available_languages",
    "revision_id",
    "release_network_id",
    "description",
    "text",
    "chunk_num",
    "section",
    "token_count",
    "page_start",
    "page_end",
    "is_reference",
    "text_for_embedding",
    "document_identity",
    "chunk_id",
    "chunk_index",
    # Document validity - the period this chunk is searchable in. Absent on
    # points written before validity existed, which search treats as current.
    "validity",
    "start_date",
    "end_date",
)


def _md5_to_uuid(hex_id: str) -> str:
    """Convert 32-char hex (or any hash) into a UUID-shaped point id."""
    h = re.sub(r"[^0-9a-fA-F]", "", hex_id or "")
    if len(h) < 32:
        h = (h + "00000000000000000000000000000000")[:32]
    else:
        h = h[:32]
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def point_id_for_record(record: dict[str, Any]) -> str:
    raw = str(record.get("_id") or "")
    if not raw:
        doc_id = str(record.get("doc_id") or "")
        chunk_num = record.get("chunk_num", 0)
        raw = f"{doc_id}:{chunk_num}"

    if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        raw,
    ):
        return raw.lower()

    if re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        return _md5_to_uuid(raw)

    import hashlib

    return _md5_to_uuid(hashlib.md5(raw.encode()).hexdigest())


def _parse_qdrant_endpoint(raw_url: str) -> dict[str, Any]:
    """Normalize Qdrant URL for reverse-proxied HTTPS endpoints.

    qdrant-client defaults HTTPS without an explicit port to :6333, which breaks
    path-based proxies such as ``https://host/qdrant``. We split host + path
    prefix and force :443 for HTTPS when no port is given.
    """
    from urllib.parse import urlparse

    parsed = urlparse(raw_url.strip())
    if not parsed.scheme or not parsed.netloc:
        # Fall back to library defaults for bare hosts / legacy values.
        return {"url": raw_url.strip(), "prefix": None, "prefer_grpc": False}

    scheme = parsed.scheme.lower()
    host = parsed.hostname or parsed.netloc
    port = parsed.port
    path = (parsed.path or "").rstrip("/")

    if port is None:
        port = 443 if scheme == "https" else 6333

    base = f"{scheme}://{host}:{port}"
    prefix = path if path and path != "/" else None
    return {"url": base, "prefix": prefix, "prefer_grpc": False}


def get_qdrant_client(
    url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> QdrantClient:
    qdrant_url = (url or os.environ.get("VECTOR_DB_URL") or "http://localhost:6333").strip()
    qdrant_key = api_key if api_key is not None else os.environ.get("VECTOR_DB_API_KEY")
    cache_key = (qdrant_url, qdrant_key)
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    is_local = any(host in qdrant_url for host in ("localhost", "127.0.0.1"))
    if not qdrant_key and not is_local:
        raise ValueError("VECTOR_DB_API_KEY is required for remote Qdrant")

    timeout = float(os.environ.get("VECTOR_DB_TIMEOUT_SECONDS", "60"))
    endpoint = _parse_qdrant_endpoint(qdrant_url)
    kwargs: dict[str, Any] = {
        "url": endpoint["url"],
        "timeout": timeout,
        "check_compatibility": False,
        "prefer_grpc": endpoint.get("prefer_grpc", False),
    }
    if endpoint.get("prefix"):
        kwargs["prefix"] = endpoint["prefix"]
    if qdrant_key:
        kwargs["api_key"] = qdrant_key

    client = QdrantClient(**kwargs)
    _client_cache[cache_key] = client
    return client


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in PAYLOAD_FIELDS:
        if key in record and record[key] is not None:
            payload[key] = record[key]

    if labels := indexed_document_labels(record):
        payload["localized_labels"] = labels
    if not payload.get("primary_language") and (language := normalize_language_tag(record.get("doc_language"))):
        payload["primary_language"] = language

    if "subjects" not in payload and record.get("crop_subjects"):
        payload["subjects"] = [{**crop, "type": "crop"} for crop in record["crop_subjects"]]
    if identity := indexed_identity(record):
        payload["document_identity"] = identity

    if "text" not in payload:
        payload["text"] = record.get("text") or ""
    return payload


def _same_cosine_vector(stored: Any, expected: list[float]) -> bool:
    if not isinstance(stored, list) or len(stored) != len(expected):
        return False
    stored_norm = math.hypot(*stored)
    expected_norm = math.hypot(*expected)
    if not 0 < stored_norm < math.inf or not 0 < expected_norm < math.inf:
        return False
    return all(
        math.isclose(left / stored_norm, right / expected_norm, abs_tol=1e-6, rel_tol=1e-5)
        for left, right in zip(stored, expected)
    )


def _hit_from_point(point: Any) -> dict[str, Any]:
    payload = _record_payload(point.payload or {})
    hit = {
        "_id": str(point.id),
        "_score": float(getattr(point, "score", 0.0) or 0.0),
        **payload,
    }
    if crops := [
        {key: value for key, value in subject.items() if key != "type"}
        for subject in payload.get("subjects", []) if subject.get("type") == "crop"
    ]:
        hit["crop_subjects"] = crops
    if hit.get("chunk_num") is not None:
        hit.setdefault("chunk_number", hit["chunk_num"])
    return hit


def _validity_conditions(on_date: str) -> list[qmodels.Filter]:
    """Conditions keeping only chunks whose validity period covers `on_date`.

    One clause per end, each satisfied either by a missing field or by a date
    on the right side of `on_date`. Read together they say: valid unless it
    has not started yet, or has already ended.

    The missing-field branches are what keep the pre-validity corpus
    searchable - points ingested before `start_date`/`end_date` existed carry
    neither, and an operator would rightly call it a regression if adding
    validity silently hid every document already in the index.

    Both ends are inclusive: a document uploaded today starts today and must
    answer today, and its end date names the last day it answers.
    """
    return [
        qmodels.Filter(
            should=[
                qmodels.IsEmptyCondition(
                    is_empty=qmodels.PayloadField(key="start_date")
                ),
                qmodels.FieldCondition(
                    key="start_date",
                    range=qmodels.DatetimeRange(lte=on_date),
                ),
            ]
        ),
        qmodels.Filter(
            should=[
                qmodels.IsEmptyCondition(
                    is_empty=qmodels.PayloadField(key="end_date")
                ),
                qmodels.FieldCondition(
                    key="end_date",
                    range=qmodels.DatetimeRange(gte=on_date),
                ),
            ]
        ),
    ]


def _exact(key: str, value: Any) -> qmodels.FieldCondition:
    """Condition: payload `key` equals `value`."""
    return qmodels.FieldCondition(key=key, match=qmodels.MatchValue(value=value))


def _scoped_condition(field: str, condition, scope: str):
    if scope == "chunk":
        return condition(field)
    differs = _exact("document_scope.fields", field)
    return qmodels.Filter(should=[
        qmodels.Filter(must=[differs, condition(f"document_scope.{field}")]),
        qmodels.Filter(must=[condition(field)], must_not=[differs]),
    ])


def _build_filter(
    doc_id: Optional[str] = None,
    chunk_num: Optional[int] = None,
    exclude_reference: bool = False,
    valid_on: Optional[str] = None,
    instance: Optional[str] = None,
    provider_id: Optional[str] = None,
    document_kind: Optional[str] = None,
    document_code: Optional[str] = None,
    release_network_id: Optional[str] = None,
    crop_subject_ids: Optional[list[str]] = None,
    allowed_instances: Optional[list[str]] = None,
    strict_language: Optional[str] = None,
    applicability: Optional[dict[str, list[str]]] = None,
    excluded_revision_ids: Optional[list[str]] = None,
    document_workflow_id: Optional[str] = None,
    metadata_filters: Optional[dict] = None,
) -> Optional[qmodels.Filter]:
    """Assemble a Qdrant filter. `valid_on` (`YYYY-MM-DD`) restricts the result
    to chunks valid on that day; None leaves validity out entirely, which is
    what the delete/list paths want - an expired chunk is still that
    document's chunk. `instance` keeps only chunks tagged with that group
    code (the API calls it `group`); None means every group."""
    must: list[qmodels.Condition] = []
    must_not: list[qmodels.Condition] = []
    if instance is not None:
        must.append(_exact("instance", instance))
    if allowed_instances is not None:
        must.append(qmodels.FieldCondition(
            key="instance", match=qmodels.MatchAny(any=allowed_instances),
        ))
    if provider_id is not None:
        must.append(_exact("provider_id", provider_id))
    if document_kind is not None:
        must.append(_exact("type", document_kind))
    if document_code is not None:
        must.append(qmodels.Filter(should=[
            _exact("document_identity.code", document_code),
            qmodels.Filter(must=[
                qmodels.IsEmptyCondition(is_empty=qmodels.PayloadField(key="document_identity.code")),
                _exact("scheme_code", document_code),
            ]),
        ]))
    if release_network_id is not None:
        must.append(_exact("release_network_id", release_network_id))
    if strict_language is not None:
        must.append(_exact("available_languages", strict_language))
    if crop_subject_ids:
        subjects = qmodels.NestedCondition(nested=qmodels.Nested(key="subjects", filter=qmodels.Filter(must=[
            _exact("type", "crop"),
            qmodels.FieldCondition(key="subjectId", match=qmodels.MatchAny(any=crop_subject_ids)),
        ])))
        legacy = qmodels.FieldCondition(key="crop_subjects[].subjectId", match=qmodels.MatchAny(any=crop_subject_ids))
        must.append(qmodels.Filter(should=[subjects, legacy]))
    contextual = []
    for key, values in (applicability or {}).items():
        if not values:
            continue
        exact = qmodels.FieldCondition(key=key, match=qmodels.MatchAny(any=values))
        if key == "coverage_area_keys":
            must_not.append(qmodels.FieldCondition(key="excluded_coverage_area_keys", match=qmodels.MatchAny(any=values)))
        if key in {"coverage_area_keys", "season_keys"}:
            contextual.append(exact)
        else:
            must.append(exact)
    if contextual:
        group_key = "applicability_groups"
        legacy_or_broad = qmodels.Filter(should=[
            _exact("applicability_scope", "broad"),
            qmodels.IsEmptyCondition(is_empty=qmodels.PayloadField(key="applicability_scope")),
        ])
        flat = [
            qmodels.Filter(should=[condition, qmodels.Filter(must=[
                legacy_or_broad, qmodels.IsEmptyCondition(is_empty=qmodels.PayloadField(key=condition.key)),
            ])])
            for condition in contextual
        ]
        group_conditions = [
            qmodels.Filter(should=[condition, qmodels.Filter(must=[
                _exact("all_areas", True), qmodels.IsEmptyCondition(is_empty=qmodels.PayloadField(key=condition.key)),
            ])])
            for condition in contextual
        ]
        must.append(qmodels.Filter(should=[
            qmodels.NestedCondition(nested=qmodels.Nested(
                key=group_key, filter=qmodels.Filter(must=group_conditions),
            )),
            qmodels.Filter(must=[qmodels.IsEmptyCondition(is_empty=qmodels.PayloadField(key=group_key)), *flat]),
        ]))
    if metadata_filters:
        scope = metadata_filters["scope"]
        for name, field in (("category", "subject_categories"), ("crop", "crop_keys")):
            if value := metadata_filters.get(name):
                must.append(_scoped_condition(field, lambda key: _exact(key, value), scope))
        area = metadata_filters.get("area")
        season = metadata_filters.get("season")
        if area:
            must_not.append(_exact("excluded_coverage_area_keys", area))
        if area or season:
            conditions: list[qmodels.Condition] = []
            if area:
                conditions.append(qmodels.Filter(should=[_exact("coverage_area_keys", area), _exact("all_areas", True)]))
            if season:
                conditions.append(_exact("season_keys", season))
            must.append(_scoped_condition(
                "applicability_groups",
                lambda key: qmodels.NestedCondition(nested=qmodels.Nested(
                    key=key, filter=qmodels.Filter(must=conditions),
                )),
                scope,
            ))
    if doc_id is not None:
        must.append(_exact("doc_id", doc_id))
    if chunk_num is not None:
        must.append(_exact("chunk_num", int(chunk_num)))
    if exclude_reference:
        must.append(_exact("is_reference", False))
    if valid_on:
        must.extend(_validity_conditions(valid_on))
    if excluded_revision_ids:
        must_not.append(qmodels.FieldCondition(key="revision_id", match=qmodels.MatchAny(any=excluded_revision_ids)))
    if document_workflow_id is not None:
        must.append(_exact("workflow_id", document_workflow_id))
    if not must and not must_not:
        return None
    return qmodels.Filter(must=must or None, must_not=must_not or None)


class QdrantVectorStore:
    backend = "qdrant"

    def __init__(
        self,
        client: Optional[QdrantClient] = None,
        clock: Optional[Clock] = None,
    ):
        self.client = client or get_qdrant_client()
        # What "today" means when filtering on document validity. Injected so a
        # test can pin the day rather than write fixtures relative to the real
        # one, and so an operator can ask what search returned on some other
        # date without changing the machine's clock.
        self.clock: Clock = clock or system_clock

    def today(self) -> str:
        """Today per the injected clock, in the `YYYY-MM-DD` payload form."""
        return today(self.clock)

    # Indexed for filtering. `start_date`/`end_date` are DATETIME so Qdrant
    # compares them as dates rather than strings; the validity filter also
    # works unindexed, so an older collection still filters correctly while
    # this is being backfilled - just more slowly.
    PAYLOAD_INDEXES = {
        "doc_id": qmodels.PayloadSchemaType.KEYWORD,
        "workflow_id": qmodels.PayloadSchemaType.KEYWORD,
        "revision_id": qmodels.PayloadSchemaType.KEYWORD,
        "release_network_id": qmodels.PayloadSchemaType.KEYWORD,
        "document_identity.code": qmodels.PayloadSchemaType.KEYWORD,
        "type": qmodels.PayloadSchemaType.KEYWORD,
        "filename": qmodels.PayloadSchemaType.KEYWORD,
        "instance": qmodels.PayloadSchemaType.KEYWORD,
        "provider_id": qmodels.PayloadSchemaType.KEYWORD,
        "available_languages": qmodels.PayloadSchemaType.KEYWORD,
        "subjects[].subjectId": qmodels.PayloadSchemaType.KEYWORD,
        "subjects[].type": qmodels.PayloadSchemaType.KEYWORD,
        "subject_categories": qmodels.PayloadSchemaType.KEYWORD,
        "agriculture_subject_ids": qmodels.PayloadSchemaType.KEYWORD,
        "coverage_area_keys": qmodels.PayloadSchemaType.KEYWORD,
        "excluded_coverage_area_keys": qmodels.PayloadSchemaType.KEYWORD,
        "season_keys": qmodels.PayloadSchemaType.KEYWORD,
        "applicability_scope": qmodels.PayloadSchemaType.KEYWORD,
        "applicability_groups[].coverage_area_keys": qmodels.PayloadSchemaType.KEYWORD,
        "applicability_groups[].season_keys": qmodels.PayloadSchemaType.KEYWORD,
        "tag_keys": qmodels.PayloadSchemaType.KEYWORD,
        "applicability_groups[].all_areas": qmodels.PayloadSchemaType.BOOL,
        "crop_keys": qmodels.PayloadSchemaType.KEYWORD,
        "document_scope.fields": qmodels.PayloadSchemaType.KEYWORD,
        "document_scope.subject_categories": qmodels.PayloadSchemaType.KEYWORD,
        "document_scope.crop_keys": qmodels.PayloadSchemaType.KEYWORD,
        "document_scope.applicability_groups[].coverage_area_keys": qmodels.PayloadSchemaType.KEYWORD,
        "document_scope.applicability_groups[].season_keys": qmodels.PayloadSchemaType.KEYWORD,
        "document_scope.applicability_groups[].all_areas": qmodels.PayloadSchemaType.BOOL,
        "chunk_num": qmodels.PayloadSchemaType.INTEGER,
        "is_reference": qmodels.PayloadSchemaType.BOOL,
        "source": qmodels.PayloadSchemaType.KEYWORD,
        "start_date": qmodels.PayloadSchemaType.DATETIME,
        "end_date": qmodels.PayloadSchemaType.DATETIME,
    }

    def _ensure_payload_indexes(self, name: str) -> None:
        """Create the filterable payload indexes, ignoring the ones that exist.

        Idempotent by design: Qdrant answers an already-present index with an
        error we can safely log and move past, which is cheaper than asking
        for the collection's index list first.
        """
        for field_name, schema in self.PAYLOAD_INDEXES.items():
            try:
                self.client.create_payload_index(
                    collection_name=name,
                    field_name=field_name,
                    field_schema=schema,
                )
            except Exception as exc:
                logger.debug("Payload index %s skipped/exists: %s", field_name, exc)

    def create_new_collection(self, name: str) -> None:
        # Qdrant atomically rejects an existing collection. Never delete it.
        if not self.client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(size=get_vector_size(), distance=qmodels.Distance.COSINE),
        ):
            raise RuntimeError("Qdrant did not create the new collection")
        self._ensure_payload_indexes(name)

    def ensure_collection(self, name: str, recreate: bool = False) -> dict[str, Any]:
        vector_size = get_vector_size()
        exists = False
        try:
            self.client.get_collection(name)
            exists = True
        except Exception:
            exists = False

        if exists and not recreate:
            # Indexes, not the collection: a collection created before a field
            # existed is missing its index, and ingest is the one path that
            # reliably runs against every live collection.
            self._ensure_payload_indexes(name)
            return {"index": name, "created": False, "backend": self.backend}

        if exists and recreate:
            self.client.delete_collection(name)

        self.client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(
                size=vector_size,
                distance=qmodels.Distance.COSINE,
            ),
        )

        self._ensure_payload_indexes(name)

        return {
            "index": name,
            "created": True,
            "backend": self.backend,
            "vector_size": vector_size,
            "distance": "Cosine",
            "message": "Qdrant collection created for passage embeddings",
        }

    def get_settings(self, name: str) -> dict[str, Any]:
        info = self.client.get_collection(name)
        vectors = info.config.params.vectors
        size = getattr(vectors, "size", None) if hasattr(vectors, "size") else None
        distance = str(getattr(vectors, "distance", "")) if vectors is not None else ""
        all_fields = [{"name": field} for field in PAYLOAD_FIELDS]
        return {
            "backend": self.backend,
            "collection": name,
            "vector_size": size,
            "distance": distance,
            "points_count": info.points_count,
            "indexed_vectors_count": getattr(info, "indexed_vectors_count", None),
            "status": str(getattr(info, "status", "")),
            "allFields": all_fields,
            "tensorFields": ["text_for_embedding"],
            "model": os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-large"),
        }

    def get_stats(self, name: str) -> dict[str, Any]:
        info = self.client.get_collection(name)
        points = info.points_count
        return {
            "backend": self.backend,
            "numberOfDocuments": points,
            "points_count": points,
            "indexed_vectors_count": getattr(info, "indexed_vectors_count", None),
            "status": str(getattr(info, "status", "")),
        }

    def upsert(
        self,
        name: str,
        records: list[dict[str, Any]],
        batch_size: int = 32,
    ) -> dict[str, Any]:
        self.ensure_collection(name, recreate=False)
        if not records:
            return {
                "records_ingested": 0,
                "index_stats": self.get_stats(name),
            }

        passages: list[str] = []
        use_e5_text = get_embedding_prompt_style() == "e5"
        for record in records:
            text_for_emb = record.get("text_for_embedding") if use_e5_text else None
            if text_for_emb is None:
                text_for_emb = record.get("text") or ""
            passages.append(str(text_for_emb))

        # If passages already include the E5 "passage:" prefix, do not double-prefix.
        use_prefix = not all(str(p or "").lower().startswith("passage:") for p in passages)
        vectors = embed_passages(passages, use_e5_prefix=use_prefix, batch_size=batch_size)
        if len(vectors) != len(records):
            raise ValueError("The embedding model did not return one vector per passage")

        points = [
            qmodels.PointStruct(
                id=point_id_for_record(record),
                vector=vector,
                payload=_record_payload(record),
            )
            for record, vector in zip(records, vectors)
        ]

        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            self.client.upsert(collection_name=name, points=batch, wait=True)
            release_points = [point for point in batch if (point.payload or {}).get("release_network_id")]
            if release_points:
                visible = self.client.retrieve(
                    collection_name=name, ids=[point.id for point in release_points],
                    with_payload=True, with_vectors=True, consistency="all",
                )
                by_id = {str(point.id): point for point in visible}
                for expected in release_points:
                    stored = by_id.get(str(expected.id))
                    if (
                        stored is None or stored.payload != expected.payload
                        or not _same_cosine_vector(stored.vector, expected.vector)
                    ):
                        raise ValueError("Qdrant could not verify the release projection")

        stats = self.get_stats(name)
        return {
            "records_ingested": len(records),
            "index_stats": stats,
            "supports_prefixed_tensor_field": True,
            "backend": self.backend,
        }

    def remove_superseded_passages(self, name: str, doc_id: str, records: list[dict[str, Any]]) -> None:
        if not records or any(row["doc_id"] != doc_id for row in records):
            raise ValueError("Replacement passages must belong to one document")
        self.client.delete(
            collection_name=name,
            points_selector=qmodels.FilterSelector(filter=qmodels.Filter(
                must=[_exact("doc_id", doc_id)],
                must_not=[qmodels.HasIdCondition(has_id=[point_id_for_record(row) for row in records])],
            )),
            wait=True,
        )

    def delete_by_doc_id(self, name: str, doc_id: str) -> dict[str, Any]:
        try:
            self.client.delete(
                collection_name=name,
                points_selector=qmodels.FilterSelector(
                    filter=_build_filter(doc_id=doc_id),
                ),
            )
            return {
                "deleted": -1,
                "doc_id": doc_id,
                "backend": self.backend,
                "mode": "filter",
            }
        except Exception as exc:
            return {
                "deleted": 0,
                "doc_id": doc_id,
                "error": str(exc),
                "backend": self.backend,
            }

    def delete_chunk(self, name: str, doc_id: str, chunk_num: int) -> dict[str, Any]:
        try:
            filt = _build_filter(doc_id=doc_id, chunk_num=chunk_num)
            points, _ = self.client.scroll(
                collection_name=name,
                scroll_filter=filt,
                limit=10,
                with_payload=False,
                with_vectors=False,
            )
            if not points:
                return {"deleted": False, "reason": "not_found", "backend": self.backend}
            ids = [p.id for p in points]
            self.client.delete(
                collection_name=name,
                points_selector=qmodels.PointIdsList(points=ids),
            )
            return {
                "deleted": True,
                "chunk_id": str(ids[0]),
                "backend": self.backend,
            }
        except Exception as exc:
            return {"deleted": False, "error": str(exc), "backend": self.backend}

    def list_by_doc_id(
        self,
        name: str,
        doc_id: str,
        limit: int = 1000,
        *,
        chunk_num: int | None = None,
        raw_payload: bool = False,
    ) -> list[dict[str, Any]]:
        filt = _build_filter(doc_id=doc_id, chunk_num=chunk_num)
        hits: list[dict[str, Any]] = []
        next_offset = None
        remaining = limit
        while remaining > 0:
            page_limit = min(remaining, 256)
            points, next_offset = self.client.scroll(
                collection_name=name,
                scroll_filter=filt,
                limit=page_limit,
                offset=next_offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                hits.append(dict(point.payload or {}) if raw_payload else _hit_from_point(point))
            remaining = limit - len(hits)
            if next_offset is None or not points:
                break
        return hits

    def get_document(self, name: str, point_id: str) -> dict[str, Any]:
        candidates = [point_id]
        if re.fullmatch(r"[0-9a-fA-F]{32}", point_id or ""):
            candidates.append(_md5_to_uuid(point_id))
        try:
            points = self.client.retrieve(
                collection_name=name,
                ids=candidates,
                with_payload=True,
                with_vectors=False,
            )
            if not points:
                raise KeyError(f"Point not found: {point_id}")
            return _hit_from_point(points[0])
        except Exception as exc:
            raise KeyError(f"Point not found: {point_id}") from exc

    def search(
        self,
        name: str,
        query: str,
        limit: int = 12,
        search_mode: str = "TENSOR",
        exclude_reference: bool = True,
        use_e5_prefix: bool = True,
        hybrid_alpha: float = 0.6,
        ef_search: int = 256,
        attributes_to_retrieve: Optional[list[str]] = None,
        apply_validity: bool = True,
        valid_on: Optional[str] = None,
        instance: Optional[str] = None,
        provider_id: Optional[str] = None,
        document_kind: Optional[str] = None,
        document_code: Optional[str] = None,
        release_network_id: Optional[str] = None,
        crop_subject_ids: Optional[list[str]] = None,
        allowed_instances: Optional[list[str]] = None,
        strict_language: Optional[str] = None,
        applicability: Optional[dict[str, list[str]]] = None,
        excluded_revision_ids: Optional[list[str]] = None,
        document_workflow_id: Optional[str] = None,
        metadata_filters: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Search `name`, by default returning only chunks valid today.

        `valid_on` (`YYYY-MM-DD`) answers the question for another day and
        `apply_validity=False` drops the period filter altogether - both exist
        for the operator who needs to see what an expired document still holds,
        and neither is what a caller serving an end user should pass.

        `instance` limits the search to one group's chunks (e.g. `mh`); None
        searches every permitted group. The same filter feeds every mode.
        """
        mode = (search_mode or "TENSOR").upper()
        as_of = (valid_on or self.today()) if apply_validity else None
        if allowed_instances == []:
            return {"hits": [], "backend": self.backend, "search_mode": mode, "valid_on": as_of}
        query_filter = _build_filter(
            exclude_reference=exclude_reference,
            valid_on=as_of,
            instance=instance,
            provider_id=provider_id,
            document_kind=document_kind,
            document_code=document_code,
            release_network_id=release_network_id,
            crop_subject_ids=crop_subject_ids,
            allowed_instances=allowed_instances,
            strict_language=strict_language,
            applicability=applicability,
            excluded_revision_ids=excluded_revision_ids,
            document_workflow_id=document_workflow_id,
            metadata_filters=metadata_filters,
        )

        if mode == "LEXICAL":
            tokens = [t.lower() for t in re.findall(r"[\w\-]+", query or "") if len(t) >= 2]
            page_limit = min(max(limit * 20, 50), 500)
            points, _ = self.client.scroll(
                collection_name=name,
                scroll_filter=query_filter,
                limit=page_limit,
                with_payload=True,
                with_vectors=False,
            )
            scored: list[tuple[float, Any]] = []
            for point in points:
                payload = point.payload or {}
                translations = (entry.get("text") or "" for entry in (payload.get("translations") or {}).values())
                blob = " ".join([payload.get("text") or "", payload.get("description") or "", *translations]).lower()
                if not tokens:
                    score = 0.0
                else:
                    score = float(sum(1.0 for tok in tokens if tok in blob))
                if score > 0:
                    scored.append((score, point))
            scored.sort(key=lambda item: item[0], reverse=True)
            hits = []
            for score, point in scored[:limit]:
                hit = _hit_from_point(point)
                hit["_score"] = score
                hits.append(hit)
            return {
                "hits": hits,
                "backend": self.backend,
                "search_mode": mode,
                "valid_on": as_of,
            }

        vector = embed_query(query, use_e5_prefix=use_e5_prefix)
        search_params = qmodels.SearchParams(hnsw_ef=ef_search) if ef_search else None
        try:
            result = self.client.query_points(
                collection_name=name,
                query=vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
                search_params=search_params,
            )
            points = result.points
        except UnexpectedResponse:
            points = self.client.search(
                collection_name=name,
                query_vector=vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
                search_params=search_params,
            )

        hits = [_hit_from_point(point) for point in points]
        if attributes_to_retrieve:
            allowed = set(attributes_to_retrieve) | {"_id", "_score"}
            trimmed = []
            for hit in hits:
                trimmed.append({k: v for k, v in hit.items() if k in allowed})
            hits = trimmed
        return {
            "hits": hits,
            "backend": self.backend,
            "search_mode": mode,
            "valid_on": as_of,
        }
