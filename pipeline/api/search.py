"""Search API: ``POST /search`` and the ranking helpers it owns.

First router split out of ``pipeline/api/__init__.py``. The vector store is
imported inside the route (not at module top) on purpose: tests patch
``pipeline.vector_store.get_vector_store`` and that only takes effect when the
name is looked up at call time.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

from fastapi import APIRouter, HTTPException

from .. import db, document_validity
from ..auth.deps import RequireSearch

router = APIRouter()

_TOKEN_RE = re.compile(r"[\w\-]+", re.UNICODE)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _tokenize(value: str) -> list[str]:
    return _TOKEN_RE.findall(_normalize_text(value))


def _token_overlap_score(query: str, text: str) -> float:
    query_tokens = set(_tokenize(query))
    text_tokens = set(_tokenize(text))
    if not query_tokens or not text_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / len(query_tokens)


def _metadata_blob(hit: dict) -> str:
    return " ".join(
        str(hit.get(key) or "")
        for key in (
            "name",
            "name_en",
            "name_gu",
            "filename",
            "title_en",
            "title_gu",
            "category_tags",
            "description",
            "doc_short_description",
            "doc_llm_description",
        )
    )


def _rank_desc(values: list[float]) -> list[int]:
    order = sorted(range(len(values)), key=lambda idx: values[idx], reverse=True)
    ranks = [0] * len(values)
    for pos, idx in enumerate(order, start=1):
        ranks[idx] = pos
    return ranks


def _expand_query(query: str, profile: str) -> str:
    q = (query or "").strip()
    mode = (profile or "none").strip().lower()
    if not q or mode in {"none", ""}:
        return q
    if mode not in {"gu-v1", "gu_v1"}:
        return q

    rules = [
        (r"ખરવા|મોવાસા|fmd", "foot and mouth disease FMD blisters lesions mouth ulcer"),
        (r"આફરો|bloat", "ruminal bloat tympany frothy bloat"),
        (r"તાવ|fever", "pyrexia febrile infection"),
        (r"કબજ|constipation", "constipation bowel obstruction laxative"),
        (r"ગળિયો|ગળાની", "throat infection pharyngitis upper respiratory"),
        (r"કૃમિ|કરમિયા|deworm", "deworming helminth anthelmintic dose"),
        (r"ગર્ભપાત|ગાભણ", "abortion pregnancy gestation prenatal feeding"),
        (r"ચરમિયા|ચામડી|ખંજવાળ|hair fall", "dermatitis skin disease mange ectoparasite tick"),
    ]

    additions: list[str] = []
    query_lower = q.lower()
    for pattern, terms in rules:
        if re.search(pattern, query_lower, flags=re.IGNORECASE):
            additions.append(terms)
    if not additions:
        return q
    return f"{q} {' '.join(additions)}".strip()


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




@router.post("/search")
async def run_search(payload: dict, user: RequireSearch):
    from ..vector_store import get_default_index_name, get_vector_store

    settings = db.get_search_settings()
    index_name = payload.get("index_name") or settings.get("indexName") or get_default_index_name()
    query = (payload.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "query is required")

    logging.info(
        "search request user=%s index=%s query=%r top_k=%s",
        user.username or user.email or user.user_id,
        index_name,
        query,
        payload.get("top_k"),
    )

    search_mode = (payload.get("search_mode") or settings.get("searchMethod") or "HYBRID").upper()
    top_k = max(1, min(int(payload.get("top_k") or settings.get("limit") or 12), 50))
    candidate_multiplier = max(1, int(payload.get("candidate_multiplier") or settings.get("candidateMultiplier") or 10))
    requested_candidate_cap = payload.get("candidate_cap")
    if requested_candidate_cap is None:
        candidate_cap = min(max(top_k * candidate_multiplier, top_k), int(settings.get("candidateCap") or 120))
    else:
        candidate_cap = int(requested_candidate_cap)
    candidate_cap = max(top_k, min(candidate_cap, 200))
    max_chunks_per_doc = max(1, int(payload.get("max_chunks_per_doc") or settings.get("maxChunksPerDoc") or 2))
    use_e5_prefix = bool(payload.get("use_e5_prefix", settings.get("useE5Prefix", True)))
    exclude_reference = bool(payload.get("exclude_reference", settings.get("excludeReference", True)))
    alpha = float(payload.get("hybrid_alpha") or settings.get("alpha") or 0.6)
    ranking_method = payload.get("ranking_method") or settings.get("rankingMethod") or "rrf"
    ef_search = int(payload.get("ef_search") or settings.get("efSearch") or 256)
    query_expansion_profile = payload.get("query_expansion_profile") or settings.get("queryExpansionProfile") or "gu-v1"
    rerank_mode = payload.get("rerank_mode") or settings.get("rerankMode") or "none"
    hybrid_rrf_k = int(payload.get("hybrid_rrf_k") or settings.get("hybridRrfK") or 60)
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
    expanded_query = _expand_query(query, query_expansion_profile)
    # Qdrant embeddings apply E5 prefixes internally; don't pre-prefix here.
    search_query = expanded_query

    store = get_vector_store()
    backend = store.backend
    # HYBRID on Qdrant currently maps to dense tensor search (lexical is separate mode).
    store_mode = "TENSOR" if backend == "qdrant" and search_mode == "HYBRID" else search_mode

    try:
        result = store.search(
            name=index_name,
            query=search_query,
            limit=candidate_cap,
            search_mode=store_mode,
            exclude_reference=exclude_reference,
            use_e5_prefix=use_e5_prefix,
            hybrid_alpha=alpha,
            ef_search=ef_search,
            apply_validity=not include_expired,
            valid_on=valid_on,
        )
    except Exception as error:
        raise HTTPException(400, f"Vector search failed ({backend}): {error}") from error

    hits = result.get("hits", [])
    hits = _rerank_hits(query, hits, rerank_mode)
    final_hits = []
    per_doc_counts: dict[str, int] = {}
    for hit in hits:
        doc_key = hit.get("doc_id") or hit.get("filename") or "__unknown__"
        if per_doc_counts.get(doc_key, 0) >= max_chunks_per_doc:
            continue
        per_doc_counts[doc_key] = per_doc_counts.get(doc_key, 0) + 1
        final_hits.append(hit)
        if len(final_hits) >= top_k:
            break

    logging.info(
        "search response user=%s index=%s query=%r candidate_count=%s final_count=%s",
        user.username or user.email or user.user_id,
        index_name,
        query,
        len(hits),
        len(final_hits),
    )

    return {
        "effective_config": {
            "index_name": index_name,
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
            "apply_validity": not include_expired,
            "valid_on": result.get("valid_on"),
            "filter_string": None,
        },
        "candidate_count": len(hits),
        "final_count": len(final_hits),
        "hits": final_hits,
        "raw_hits": hits if payload.get("include_raw_hits") else None,
    }
