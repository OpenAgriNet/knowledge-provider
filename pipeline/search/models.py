"""Validated public retrieval controls, independent of the vector transport."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pipeline.metadata.extraction import MetadataFilters
from pipeline.metadata.knowledge_metadata import KnowledgeFilters
from pipeline.search.ranking import RerankMode

Identifier = Annotated[str, Field(min_length=1, max_length=256)]
ContextValue = Annotated[str, Field(min_length=1, max_length=100)]


class AdvisoryContext(BaseModel):
    """The farmer's situation for the advisory profile. The crop filters;
    season, crop stage and place only reorder."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    crop: ContextValue | None = None
    season: ContextValue | None = None
    days_after_sowing: int | None = Field(None, ge=-3650, le=3650)
    state_lgd_code: int | None = Field(None, ge=0)
    district_lgd_code: int | None = Field(None, ge=0)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, allow_inf_nan=False)

    query: str = Field(min_length=1, max_length=4096)
    # "scheme" answers scheme questions the BV way: resolve the scheme the
    # question names, steer retrieval to the asked section, and widen to all
    # schemes when the resolved scheme has no indexed passages. "advisory"
    # keeps one passage per advisory entry and prefers the farmer's season,
    # crop stage and place.
    search_profile: Literal["default", "scheme", "advisory"] = "default"
    advisory: AdvisoryContext | None = None
    network_id: Identifier | None = None
    index_name: Identifier | None = None
    document_kind: Identifier | None = None
    document_code: Identifier | None = None
    document_workflow_id: Identifier | None = None
    group: Identifier | None = None
    provider_id: Identifier | None = None
    language: str | None = None
    strict_language: bool = False
    top_k: int | None = Field(None, ge=1, le=50)
    candidate_cap: int | None = Field(None, ge=1, le=200)
    candidate_multiplier: int | None = Field(None, ge=1, le=100)
    max_chunks_per_doc: int | None = Field(None, ge=1, le=50)
    ef_search: int | None = Field(None, ge=1, le=10000)
    search_mode: Literal["HYBRID", "TENSOR", "LEXICAL"] | None = None
    hybrid_alpha: float | None = Field(None, ge=0, le=1)
    hybrid_rrf_k: int | None = Field(None, ge=1, le=1000)
    ranking_method: Literal["rrf", "normalize_linear"] | None = None
    rerank_mode: RerankMode | None = None
    query_expansion_profile: str | None = Field(None, max_length=128)
    use_e5_prefix: bool | None = None
    exclude_reference: bool | None = None
    include_expired: bool = False
    include_raw_hits: bool = False
    valid_on: str | None = Field(None, max_length=32)
    crop_subject_ids: list[str] = Field(default_factory=list, max_length=32)
    applicability: KnowledgeFilters = Field(default_factory=KnowledgeFilters)
    metadata_filters: MetadataFilters = Field(default_factory=MetadataFilters)

    @field_validator("search_mode", mode="before")
    @classmethod
    def normalize_mode(cls, value):
        return value.upper() if isinstance(value, str) else value

    @field_validator("group", "provider_id", mode="before")
    @classmethod
    def optional_scope(cls, value):
        return value.strip() or None if isinstance(value, str) else value

    @field_validator("document_kind", "document_code")
    @classmethod
    def normalize_identity(cls, value):
        return value.lower() if value is not None else None

    @model_validator(mode="after")
    def identity_scope(self):
        if self.search_profile in ("scheme", "advisory") and self.document_kind not in (None, self.search_profile):
            raise ValueError(f"The {self.search_profile} search profile searches only {self.search_profile} documents")
        if self.advisory is not None and self.search_profile != "advisory":
            raise ValueError("advisory context requires the advisory search profile")
        if self.document_code and not self.document_kind and self.search_profile != "scheme":
            raise ValueError("document_code requires document_kind")
        if self.network_id and (self.include_expired or self.valid_on or self.include_raw_hits):
            raise ValueError("Network search cannot override current validity or request raw diagnostic hits")
        return self
