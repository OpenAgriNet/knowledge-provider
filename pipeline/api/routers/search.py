"""Search API: ``POST /search`` and the ranking helpers it owns.

First router split out of ``pipeline/api/api.py``. The vector store is
imported inside the route (not at module top) on purpose: tests patch
``pipeline.vector_store.get_vector_store`` and that only takes effect when the
name is looked up at call time.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import unicodedata
from collections import Counter

from fastapi import APIRouter, HTTPException

from pipeline import db
from pipeline.auth.deps import RequireSearch
from pipeline.auth.tenancy import allowed_instances, assert_instance_access
from pipeline.documents import document_validity
from pipeline.metadata.crop_metadata import parse_crop_subject_ids
from pipeline.metadata.extraction import MetadataFilters
from pipeline.metadata.keys import normalized_key
from pipeline.metadata.knowledge_metadata import KnowledgeFilters
from pipeline.metadata.localization import localize_search_hit, normalize_language_tag
from pipeline.network.release_policy import managed_release_collections, managed_release_targets
from pipeline.search import advisory_query
from pipeline.search.access import (
    is_network_principal,
    network_search_collection,
    principal_networks,
)
from pipeline.search.document_identity import indexed_identity
from pipeline.search.models import SearchRequest
from pipeline.search.query_expansion import expand_query
from pipeline.search.scheme_query import (
    QUERY_EXPANSIONS,
    classify_query_intent,
    classify_section_focus,
    expand_query_for_search,
    finalize_scheme_hits,
    load_scheme_registry,
    query_names_unindexed_scheme,
    rerank_scheme_hits,
    resolve_scheme_code,
    supplemental_section,
    with_section_types,
)

router = APIRouter()

def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _tokenize(value: str) -> list[str]:
    text = unicodedata.normalize("NFC", _normalize_text(value))
    return "".join(
        character if unicodedata.category(character)[0] in "LMN" or character == "-" else " "
        for character in text
    ).split()


def _token_overlap_score(query: str, text: str) -> float:
    query_tokens = set(_tokenize(query))
    text_tokens = set(_tokenize(text))
    if not query_tokens or not text_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / len(query_tokens)


def _metadata_blob(hit: dict) -> str:
    labels = [label for key in ("topics", "document_tags", "season_keys", "subject_categories")
              for label in (hit.get(key) or [])]
    localized = [str(value or "") for entry in (hit.get("localized_labels") or {}).values()
                 for value in (entry.get("title"), entry.get("description"))]
    fields = (
        str(hit.get(key) or "")
        for key in (
            "name",
            "filename",
            "description",
        )
    )
    return " ".join([*fields, *labels, *localized])


def _display_search_hit(hit: dict, requested_language: str | None) -> dict:
    """Present localized document labels and the legacy search response aliases."""
    name = hit.get("name")
    result = dict(hit)
    if hit.get("type") == "scheme" or hit.get("scheme_code"):
        if identity := indexed_identity(hit):
            result["scheme_code"] = identity.get("code")
            result["scheme_name"] = identity.get("name") or name
            result["scheme_aliases"] = identity.get("aliases") or []
    if not name:
        return result
    variants = hit.get("localized_labels") or {}
    selected = variants.get(hit.get("text_language"), {}) if requested_language else {}
    result.setdefault("name_en", name)
    result.setdefault("name_gu", variants.get("gu", {}).get("title") or name)
    result.setdefault("title_en", variants.get("en", {}).get("title") or "")
    result.setdefault("title_gu", variants.get("gu", {}).get("title") or "")
    result.setdefault("doc_language", hit.get("primary_language") or "")
    result.setdefault("category_tags", ", ".join(hit.get("document_tags", [])))
    result.setdefault("doc_short_description", hit.get("description") or "")
    result.setdefault("doc_llm_description", "")
    result.setdefault("ingestion_status", "")
    result.setdefault("quality_score", 0.0)
    result.setdefault("priority_rank", 0.0)
    if selected.get("title"):
        result["name"] = selected["title"]
    if selected.get("description"):
        result["description"] = selected["description"]
    return result


def _rank_desc(values: list[float]) -> list[int]:
    order = sorted(range(len(values)), key=lambda idx: values[idx], reverse=True)
    ranks = [0] * len(values)
    for pos, idx in enumerate(order, start=1):
        ranks[idx] = pos
    return ranks


def _bm25lite_scores(query: str, docs: list[str]) -> list[float]:
    query_tokens = _tokenize(query)
    if not query_tokens or not docs:
        return [0.0] * len(docs)

    doc_tokens = [_tokenize(doc) for doc in docs]
    avg_len = max(1.0, sum(len(tokens) for tokens in doc_tokens) / max(1, len(doc_tokens)))
    df: Counter[str] = Counter()
    for tokens in doc_tokens:
        for token in set(tokens):
            df[token] += 1

    k1 = 1.2
    b = 0.75
    scores: list[float] = []
    for tokens in doc_tokens:
        tf = Counter(tokens)
        dl = len(tokens)
        norm = k1 * (1 - b + b * dl / avg_len)
        score = 0.0
        for term in query_tokens:
            if term not in tf:
                continue
            idf = math.log(1.0 + (len(doc_tokens) - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * (tf[term] * (k1 + 1.0)) / (tf[term] + norm)
        scores.append(score)
    return scores


def _rank_hybrid_candidates(query: str, hits: list[dict], alpha: float, method: str, rank_offset: int) -> list[dict]:
    if len(hits) < 2 or alpha == 1:
        return hits
    lexical = _bm25lite_scores(query, [f"{hit.get('text', '')} {_metadata_blob(hit)}" for hit in hits])
    if not any(lexical):
        return hits
    dense = [float(hit.get("_score") or 0) for hit in hits]
    if method == "normalize_linear":
        dense_min, dense_max = min(dense), max(dense)
        dense = [(score - dense_min) / (dense_max - dense_min or 1) for score in dense]
        lexical = [score / max(lexical) for score in lexical]
    else:
        dense = [1 / (rank_offset + rank) for rank in _rank_desc(dense)]
        lexical = [1 / (rank_offset + rank) if score else 0 for rank, score in zip(_rank_desc(lexical), lexical)]
    scored = [
        {**hit, "_hybrid_score": alpha * semantic + (1 - alpha) * keyword}
        for hit, semantic, keyword in zip(hits, dense, lexical)
    ]
    return sorted(scored, key=lambda hit: hit["_hybrid_score"], reverse=True)


def _rerank_hits(query: str, hits: list[dict], rerank_mode: str) -> list[dict]:
    mode = (rerank_mode or "none").strip().lower()
    if mode in {"", "none"} or not hits:
        return hits

    raw_scores = [float(hit.get("_score", hit.get("score", 0.0)) or 0.0) for hit in hits]
    min_score = min(raw_scores)
    max_score = max(raw_scores)
    denom = (max_score - min_score) if max_score > min_score else 1.0
    semantic_scores = [(raw - min_score) / denom for raw in raw_scores]
    text_scores = [_token_overlap_score(query, str(hit.get("text") or "")) for hit in hits]
    meta_scores = [_token_overlap_score(query, _metadata_blob(hit)) for hit in hits]

    rescored: list[dict] = []
    if mode == "bm25lite":
        docs = [f"{str(hit.get('text') or '')} {_metadata_blob(hit)}".strip() for hit in hits]
        bm_scores = _bm25lite_scores(query, docs)
        bm_min = min(bm_scores) if bm_scores else 0.0
        bm_max = max(bm_scores) if bm_scores else 1.0
        bm_denom = (bm_max - bm_min) if bm_max > bm_min else 1.0
        bm_norm = [(score - bm_min) / bm_denom for score in bm_scores]
        for hit, semantic, bm25, meta in zip(hits, semantic_scores, bm_norm, meta_scores):
            enriched = dict(hit)
            enriched["_rerank_score"] = (0.50 * semantic) + (0.40 * bm25) + (0.10 * meta) + (-0.10 if bool(hit.get("is_reference", False)) else 0.0)
            rescored.append(enriched)
    elif mode in {"rrf-lite", "rrf_lite", "rrf"}:
        sem_rank = _rank_desc(semantic_scores)
        text_rank = _rank_desc(text_scores)
        meta_rank = _rank_desc(meta_scores)
        k = 30
        for idx, hit in enumerate(hits):
            enriched = dict(hit)
            enriched["_rerank_score"] = (1.0 / (k + sem_rank[idx])) + (1.0 / (k + text_rank[idx])) + (1.0 / (k + meta_rank[idx]))
            rescored.append(enriched)
    else:
        for hit, semantic, text_score, meta in zip(hits, semantic_scores, text_scores, meta_scores):
            enriched = dict(hit)
            enriched["_rerank_score"] = (0.60 * semantic) + (0.30 * max(text_score, meta)) + (0.10 * meta)
            rescored.append(enriched)

    rescored.sort(key=lambda hit: float(hit.get("_rerank_score", 0.0)), reverse=True)
    return rescored




def _requested_group(payload: dict) -> str | None:
    raw = payload.get("group")
    if raw is not None and not isinstance(raw, str):
        raise HTTPException(400, "group must be a string")
    return (raw or "").strip().lower() or None


def _requested_provider_id(payload: dict) -> str | None:
    raw = payload.get("provider_id")
    if raw is not None and not isinstance(raw, str):
        raise HTTPException(400, "provider_id must be a string")
    return (raw or "").strip().lower() or None


@router.get("/search/collections")
async def search_collections(user: RequireSearch):
    from pipeline.vector_store import get_default_index_name

    try:
        scopes = managed_release_collections()
        advisories = managed_release_targets("advisory")
        schemes = managed_release_targets("scheme")
    except ValueError as exc:
        raise HTTPException(503, "Network collections are misconfigured") from exc
    if not scopes:
        return [] if is_network_principal(user) else [{"label": "Documents", "name": get_default_index_name()}]
    labels = {collection: "Published schemes" for collection in schemes.values()}
    labels.update({collection: "Published advisories" for collection in advisories.values()})
    available = [
        {"label": labels.get(collection, "Published documents"), "name": collection, "network_id": network_id,
         **({"document_kind": "scheme"} if collection in schemes.values() and collection not in advisories.values() else {})}
        for collection, network_id in scopes.items()
        if network_id in principal_networks(user)
    ]
    if not is_network_principal(user):
        available.insert(0, {"label": "Local documents", "name": get_default_index_name()})
    return available


@router.post("/search/", include_in_schema=False)
@router.post("/search")
async def run_search(payload: SearchRequest, user: RequireSearch):
    """Search passages visible to the caller, optionally within one provider."""
    from pipeline.vector_store import get_default_index_name, get_vector_store

    payload = payload.model_dump(exclude_none=True)
    settings = db.get_search_settings()
    from pipeline.network.release_policy import index_only_mode

    index_name = get_default_index_name()
    if index_only_mode() and payload.get("index_name") not in (None, index_name):
        raise HTTPException(422, "Local search uses only the configured serving collection")
    if not index_only_mode():
        index_name = payload.get("index_name") or index_name
    release_network = payload.get("network_id")
    scheme_profile = payload.get("search_profile") == "scheme"
    advisory_profile = payload.get("search_profile") == "advisory"
    document_kind = "scheme" if scheme_profile else "advisory" if advisory_profile else payload.get("document_kind")
    document_code = payload.get("document_code")
    try:
        if release_network:
            index_name = network_search_collection(user, release_network, document_kind, payload.get("index_name"))
        elif is_network_principal(user):
            raise HTTPException(422, "network_id is required for a network search principal")
        elif index_name in managed_release_collections():
            raise HTTPException(422, "network_id is required for a network serving collection")
        elif index_name != get_default_index_name():
            raise HTTPException(422, "Local search uses only the configured serving collection")
    except ValueError as exc:
        raise HTTPException(503, "Search network policy is misconfigured") from exc
    raw_query = payload.get("query")
    if raw_query is not None and not isinstance(raw_query, str):
        raise HTTPException(422, "query must be text")
    query = (raw_query or "").strip()
    if not query:
        raise HTTPException(400, "query is required")
    if len(query) > 4096:
        raise HTTPException(422, "query must be at most 4096 characters")

    requested_language = payload.get("language")
    language = normalize_language_tag(requested_language)
    if requested_language is not None and language is None:
        raise HTTPException(422, "language must be a BCP 47 tag")
    strict_language = payload.get("strict_language", False)
    if type(strict_language) is not bool or (strict_language and not language):
        raise HTTPException(422, "strict_language requires a boolean and a language when true")

    logging.info(
        "search request",
        extra={
            "user_id": user.user_id,
            "index": index_name,
            "query_length": len(query),
            "top_k": payload.get("top_k"),
        },
    )

    # The scheme profile keeps the BV provider's retrieval shape unless the
    # caller overrides it: dense search, ten answers drawn from max(8x, 40)
    # candidates, and no per-document cap so one guideline can fill the answer.
    # The advisory profile keeps the BV provider's shape too: dense search, five
    # answers (ADVISORY_QDRANT_TOP_K overrides) from forty candidates.
    if scheme_profile:
        profile_defaults = {"searchMethod": "TENSOR", "limit": 10}
    elif advisory_profile:
        advisory_top_k = os.environ.get("ADVISORY_QDRANT_TOP_K", "").strip()
        profile_defaults = {"searchMethod": "TENSOR", "limit": int(advisory_top_k) if advisory_top_k.isdigit() else 5}
    else:
        profile_defaults = {}
    search_mode = (payload.get("search_mode") or profile_defaults.get("searchMethod")
                   or settings.get("searchMethod") or "HYBRID").upper()
    top_k = max(1, min(int(payload.get("top_k") or profile_defaults.get("limit") or settings.get("limit") or 12), 50))
    candidate_multiplier = max(1, int(payload.get("candidate_multiplier") or settings.get("candidateMultiplier") or 10))
    requested_candidate_cap = payload.get("candidate_cap")
    if requested_candidate_cap is None and advisory_profile:
        candidate_cap = 40
    elif requested_candidate_cap is None and scheme_profile:
        candidate_cap = max(top_k * 8, 40)
    elif requested_candidate_cap is None:
        candidate_cap = min(max(top_k * candidate_multiplier, top_k), int(settings.get("candidateCap") or 120))
    else:
        candidate_cap = int(requested_candidate_cap)
    candidate_cap = max(top_k, min(candidate_cap, 200))
    # None = no per-document cap: section balancing must see every candidate.
    max_chunks_per_doc = payload.get("max_chunks_per_doc") or (
        None if scheme_profile or advisory_profile else settings.get("maxChunksPerDoc") or 2)
    max_chunks_per_doc = max(1, int(max_chunks_per_doc)) if max_chunks_per_doc is not None else None
    use_e5_prefix = bool(payload.get("use_e5_prefix", settings.get("useE5Prefix", True)))
    exclude_reference = bool(payload.get("exclude_reference", settings.get("excludeReference", True)))
    alpha = float(payload.get("hybrid_alpha", settings.get("alpha", 0.6)))
    if not 0 <= alpha <= 1:
        raise HTTPException(422, "hybrid_alpha must be between 0 and 1")
    ranking_method = payload.get("ranking_method") or settings.get("rankingMethod") or "rrf"
    ef_search = int(payload.get("ef_search") or settings.get("efSearch") or 256)
    query_expansion_profile = payload.get("query_expansion_profile") or settings.get("queryExpansionProfile") or "none"
    rerank_mode = payload.get("rerank_mode") or settings.get("rerankMode") or "none"
    hybrid_rrf_k = max(1, min(int(payload.get("hybrid_rrf_k") or settings.get("hybridRrfK") or 60), 1000))
    # Validity is on by default: a search serving a farmer must not answer from
    # a document that has expired or has not started. Both overrides are
    # operator tools - `include_expired` to inspect what an expired document
    # still holds, `valid_on` to ask what search would have returned on another
    # day - and the response echoes which day was actually applied.
    include_expired = bool(payload.get("include_expired", False))
    valid_on = (payload.get("valid_on") or "").strip() or None
    if valid_on:
        try:
            # Reassigned, not just checked: parse_day returns the canonical
            # zero-padded form, and the vector store's date range rejects the
            # `2026-9-3` spelling that strptime happily accepts.
            valid_on = document_validity.parse_day(valid_on, "valid_on")
        except document_validity.DocumentValidityError as exc:
            raise HTTPException(400, str(exc)) from None
    group = _requested_group(payload)
    permitted = allowed_instances(user)
    if permitted is not None and not permitted:
        raise HTTPException(403, "No provider access")
    if group is not None:
        assert_instance_access(user, group)
    scope_filter = {"allowed_instances": sorted(permitted)} if group is None and permitted is not None else {}
    provider_id = _requested_provider_id(payload)
    selected_workflow = payload.get("document_workflow_id")
    if selected_workflow is not None and (
        not isinstance(selected_workflow, str) or not 1 <= len(selected_workflow) <= 256
    ):
        raise HTTPException(422, "document_workflow_id must be a workflow identifier")
    try:
        crop_subject_ids = parse_crop_subject_ids(payload.get("crop_subject_ids"))
    except ValueError as exc:
        raise HTTPException(400, "crop_subject_ids must contain at most 32 absolute URI strings") from exc
    try:
        applicability = KnowledgeFilters.model_validate(payload.get("applicability", {})).qdrant_values()
        metadata_filters = MetadataFilters.model_validate(payload.get("metadata_filters", {})).qdrant_values()
    except ValueError as exc:
        raise HTTPException(422, "Invalid agriculture applicability filters") from exc
    advisory_context = payload.get("advisory") or {}
    advisory_crop = advisory_query.normalize_crop(advisory_context.get("crop")) if advisory_profile else ""
    if advisory_crop:
        # The crop is the advisory profile's one hard filter (BV parity).
        metadata_filters = {"scope": "document", **metadata_filters, "crop": normalized_key(advisory_crop)}
    try:
        expanded_query = expand_query(query, query_expansion_profile)
    except (TypeError, ValueError) as exc:
        raise HTTPException(503, "Search expansion is misconfigured") from exc
    # Qdrant embeddings apply E5 prefixes internally; don't pre-prefix here.
    search_query = expanded_query
    scheme_search = None
    scheme_registry: list = []
    if scheme_profile:
        try:
            scheme_registry = await asyncio.to_thread(
                load_scheme_registry, network_id=release_network, group=group, permitted=permitted,
            )
        except Exception as error:
            logging.warning("scheme registry unavailable error_type=%s", type(error).__name__)
        known_codes = {entry["scheme_code"] for entry in scheme_registry}
        # A code the caller sends is trusted when the registry knows it, or when
        # the registry cannot be read; otherwise the question text decides.
        if document_code and (not known_codes or document_code in known_codes):
            code_source = "request"
        else:
            document_code = resolve_scheme_code(query, scheme_registry)
            code_source = "query" if document_code else None
        intent = classify_query_intent(query)
        section_focus = classify_section_focus(query)
        search_query = expand_query_for_search(expanded_query, intent, section_focus)
        scheme_search = {
            "resolved_scheme_code": document_code, "scheme_code_source": code_source,
            "effective_scheme_code": document_code, "widened_to_all_schemes": False,
            "query_intent": intent, "section_focus": section_focus,
            "supplemental_section": None, "supplemental_hits": 0,
            # An empty registry means the catalog could not be read; never
            # treat that as "the question names an unknown scheme".
            "unindexed_scheme": bool(not document_code and scheme_registry
                                     and query_names_unindexed_scheme(query, scheme_registry)),
        }

    store = get_vector_store()
    backend = store.backend
    # HYBRID on Qdrant currently maps to dense tensor search (lexical is separate mode).
    store_mode = "TENSOR" if backend == "qdrant" and search_mode == "HYBRID" else search_mode

    try:
        exclusions = await asyncio.to_thread(db.inactive_network_revision_ids, release_network, index_name) if release_network else []
        search_arguments = dict(
            name=index_name,
            query=search_query,
            search_mode=store_mode,
            exclude_reference=exclude_reference,
            use_e5_prefix=use_e5_prefix,
            hybrid_alpha=alpha,
            ef_search=ef_search,
            apply_validity=not include_expired,
            valid_on=valid_on,
            instance=group,
            provider_id=provider_id,
            **({"document_kind": document_kind} if document_kind else {}),
            **({"document_code": document_code} if document_code else {}),
            **({"release_network_id": release_network} if release_network else {}),
            **({"strict_language": language} if strict_language else {}),
            **scope_filter,
            **({"document_workflow_id": selected_workflow} if selected_workflow else {}),
            **({"crop_subject_ids": crop_subject_ids} if crop_subject_ids else {}),
            **({"applicability": applicability} if applicability else {}),
            **({"metadata_filters": metadata_filters} if metadata_filters else {}),
            **({"excluded_revision_ids": exclusions} if exclusions else {}),
        )
        validate_hits = db.visible_release_hits if release_network else db.visible_document_hits
        visibility_scope = {"network_id": release_network} if release_network else {"index_name": index_name}

        async def retrieve(arguments: dict) -> tuple[dict, list[dict]]:
            result = await asyncio.to_thread(store.search, limit=candidate_cap, **arguments)
            candidates = result.get("hits", [])
            hits = await asyncio.to_thread(validate_hits, candidates, **visibility_scope)
            if (index_only_mode() or release_network) and len(candidates) == candidate_cap and len(hits) < top_k:
                result = await asyncio.to_thread(store.search, limit=min(candidate_cap * 3, 600), **arguments)
                hits = await asyncio.to_thread(validate_hits, result.get("hits", []), **visibility_scope)
            return result, hits

        if scheme_search and scheme_search["unindexed_scheme"]:
            result, hits = {}, []
        else:
            result, hits = await retrieve(search_arguments)
            if scheme_search and document_code and not hits:
                # The catalog and the indexed passages can disagree about a
                # code. Answer from the question across all schemes rather
                # than returning nothing; the response says it widened.
                search_arguments.pop("document_code", None)
                result, hits = await retrieve(search_arguments)
                scheme_search.update(effective_scheme_code=None, widened_to_all_schemes=True)
            if scheme_search:
                hits = with_section_types(hits)
                needed = supplemental_section(scheme_search["section_focus"], scheme_search["query_intent"])
                if needed and not any(hit["section_type"] == needed for hit in hits):
                    # The asked section is missing (e.g. exclusions for an
                    # eligibility question): search once more with its words.
                    extra_query = f"{expanded_query} {QUERY_EXPANSIONS[needed]}"
                    _, extra = await retrieve({**search_arguments, "query": extra_query})
                    known_keys = {hit.get("chunk_id") or hit.get("_id") for hit in hits}
                    added = [hit for hit in with_section_types(extra) if (hit.get("chunk_id") or hit.get("_id")) not in known_keys]
                    hits += added
                    scheme_search.update(supplemental_section=needed, supplemental_hits=len(added))
    except Exception as error:
        logging.warning("vector search failed backend=%s error_type=%s", backend, type(error).__name__)
        raise HTTPException(503, "Vector search is temporarily unavailable") from error

    if language:
        hits = [
            localized for hit in hits
            if (localized := localize_search_hit(hit, language, strict=strict_language)) is not None
        ]
    hits = [_display_search_hit(hit, language) for hit in hits]
    if search_mode == "HYBRID" and backend == "qdrant":
        hits = _rank_hybrid_candidates(query, hits, alpha, ranking_method, hybrid_rrf_k)
    hits = _rerank_hits(query, hits, rerank_mode)
    if scheme_search and not scheme_search["effective_scheme_code"] and scheme_registry:
        known_codes = {entry["scheme_code"] for entry in scheme_registry}
        hits = [hit for hit in hits if (hit.get("scheme_code") or "").lower() in known_codes]
    if scheme_search:
        hits = rerank_scheme_hits(query, hits, scheme_search["query_intent"])
    if advisory_profile:
        season = advisory_query.preferred_season(advisory_context.get("season"))
        days_after_sowing = advisory_context.get("days_after_sowing")
        state, district = advisory_context.get("state_lgd_code"), advisory_context.get("district_lgd_code")
        hits = advisory_query.rank_advisory_hits(
            advisory_query.dedupe_by_entry(hits),
            season=season, days_after_sowing=days_after_sowing, state=state, district=district,
        )
        hits = [{**hit, "origin": hit.get("source") or "docs-pipeline"} for hit in hits]
    final_hits = []
    per_doc_counts: dict[str, int] = {}
    for hit in hits:
        doc_key = hit.get("doc_id") or hit.get("filename") or "__unknown__"
        if max_chunks_per_doc is not None and per_doc_counts.get(doc_key, 0) >= max_chunks_per_doc:
            continue
        per_doc_counts[doc_key] = per_doc_counts.get(doc_key, 0) + 1
        final_hits.append(hit)
        # The scheme profile chooses its top_k after section balancing below.
        if len(final_hits) >= top_k and not scheme_search:
            break
    if scheme_search:
        final_hits = finalize_scheme_hits(final_hits, scheme_search["section_focus"], scheme_search["query_intent"], top_k)

    scheme_candidates = {}
    query_tokens = set(_tokenize(query))
    for hit in hits:
        code = hit.get("scheme_code")
        workflow_id = hit.get("workflow_id")
        name = hit.get("scheme_name") or hit.get("name")
        aliases = hit.get("scheme_aliases") or []
        if not all(isinstance(item, str) and item for item in (code, workflow_id, name)):
            continue
        labels = [code, name, *(alias for alias in aliases if isinstance(alias, str))]
        # A broad topical query can return many related schemes. Ask the user
        # to choose when their words refer to more than one actual scheme label.
        if any((tokens := set(_tokenize(label))) and tokens <= query_tokens for label in labels):
            scheme_candidates.setdefault(workflow_id, {
                "workflow_id": workflow_id, "name": name, "code": code, "provider": hit.get("instance"),
            })
        if len(scheme_candidates) == 5:
            break
    choices = list(scheme_candidates.values()) if len(scheme_candidates) > 1 and not selected_workflow else []

    logging.info(
        "search response",
        extra={
            "user_id": user.user_id,
            "index": index_name,
            "candidate_count": len(hits),
            "final_count": len(final_hits),
        },
    )
    score_field = next((field for field in ("_scheme_score", "_advisory_score", "_rerank_score", "_hybrid_score")
                        if final_hits and all(field in hit for hit in final_hits)), "_score")
    advisory_search = None
    if advisory_profile:
        advisory_search = {
            "status": "success" if final_hits else "not_found",
            "message": None if final_hits else "No advisory found for this question",
            "crop": advisory_crop or None,
            "preferred_season": season or None,
            "location_status": advisory_query.location_status(final_hits, state is not None or district is not None),
            "has_day_range": any(advisory_query.has_day_range(hit) for hit in final_hits),
            "days_matched": any(advisory_query.days_in_range(hit, days_after_sowing) for hit in final_hits),
            "has_location_specific": any(not advisory_query.is_national(hit) for hit in final_hits),
        }
    if scheme_search:
        unindexed = scheme_search.pop("unindexed_scheme")
        if final_hits:
            scheme_search.update(status="success", message=None)
        elif scheme_search["resolved_scheme_code"]:
            scheme_search.update(status="not_found", message="No passages matched the scheme")
        else:
            scheme_search.update(status="scheme_unavailable", message=(
                "Scheme not available in the document index" if unindexed else "No scheme passages matched"))

    return {
        "effective_config": {
            "search_profile": payload.get("search_profile", "default"),
            "index_name": index_name,
            "network_id": release_network,
            "document_kind": document_kind,
            "document_code": document_code,
            "backend": backend,
            "query": query,
            "search_mode": search_mode,
            "store_search_mode": store_mode,
            "top_k": top_k,
            "candidate_cap": candidate_cap,
            "candidate_multiplier": candidate_multiplier,
            "max_chunks_per_doc": max_chunks_per_doc,
            "use_e5_prefix": use_e5_prefix,
            "exclude_reference": exclude_reference,
            "hybrid_alpha": alpha,
            "ranking_method": ranking_method,
            "hybrid_rrf_k": hybrid_rrf_k,
            "ef_search": ef_search,
            "query_expansion_profile": query_expansion_profile,
            "query_expansion_applied": expanded_query != query,
            "rerank_mode": rerank_mode,
            "ranking_score_field": score_field,
            "apply_validity": not include_expired,
            "valid_on": result.get("valid_on"),
            "group": group,
            "provider_id": provider_id,
            "crop_subject_ids": crop_subject_ids,
            "applicability": applicability,
            "language": language,
            "strict_language": strict_language,
            "filter_string": None,
        },
        "candidate_count": len(hits),
        "final_count": len(final_hits),
        "hits": final_hits,
        "scheme_choices": choices,
        "scheme_search": scheme_search,
        "advisory_search": advisory_search,
        "raw_hits": hits if payload.get("include_raw_hits") else None,
    }
