"""Names for implemented ranking strategies and external compatibility inputs."""

from typing import Annotated, Literal

from pydantic import BeforeValidator


def normalize_rerank_mode(value):
    if isinstance(value, str):
        value = value.strip().lower()
        return {"cross-encoder": "heuristic", "colbert": "heuristic", "rrf": "rrf-lite", "rrf_lite": "rrf-lite"}.get(value, value)
    return value


RerankMode = Annotated[Literal["none", "bm25lite", "rrf-lite", "heuristic"], BeforeValidator(normalize_rerank_mode)]
