"""Document metadata shared by intake and retrieval."""

import json
from datetime import datetime
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
)

from pipeline.metadata.crop_metadata import (
    AgricultureSubjectReference,
    CropSubjects,
    parse_crop_subject_ids,
    parse_crop_subjects,
)
from pipeline.metadata.intake_profiles import ProfileID, ProfilePin
from pipeline.metadata.keys import season_key
from pipeline.metadata.localization import normalize_language_tag
from pipeline.metadata.schema_packs import SchemaSelection

Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class LocalizedDocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)] | None = None
    description: Description | None = None


class AdministrativeArea(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    codeScheme: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    areaCode: str = Field(min_length=1, max_length=128)
    areaLevel: Label | None = None
    areaName: Label | None = None

    def lookup_key(self) -> str:
        return f"{self.codeScheme.casefold()}:{self.areaCode.casefold()}"


class ApplicabilityGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage_areas: list[AdministrativeArea] = Field(min_length=1, max_length=32)
    seasons: list[Label] = Field(min_length=1, max_length=12)

    def index_value(self) -> dict[str, list[str]]:
        return {
            "coverage_area_keys": list(dict.fromkeys(area.lookup_key() for area in self.coverage_areas)),
            "season_keys": list(dict.fromkeys(season_key(season) for season in self.seasons)),
        }


def _bounded_attributes(attributes: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if len(json.dumps(attributes, ensure_ascii=False, allow_nan=False).encode()) > 32_768:
        raise ValueError("Attributes must be at most 32,768 bytes")
    return attributes


class TypedSubject(AgricultureSubjectReference):
    type: ProfileID
    attributes: dict[str, JsonValue] = Field(default_factory=dict, max_length=128)

    _check_attributes = field_validator("attributes")(_bounded_attributes)


class KnowledgeMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: Description | None = None
    localized: dict[str, LocalizedDocumentMetadata] = Field(default_factory=dict, max_length=16)
    subject_categories: list[Label] = Field(default_factory=list, max_length=32)
    agriculture_subjects: CropSubjects = Field(default_factory=list)
    subjects: list[TypedSubject] = Field(default_factory=list, max_length=32)
    coverage_areas: list[AdministrativeArea] = Field(default_factory=list, max_length=32)
    excluded_coverage_areas: list[AdministrativeArea] = Field(default_factory=list, max_length=32)
    seasons: list[Label] = Field(default_factory=list, max_length=12)
    broad_applicability: bool = False
    applicability_groups: list[ApplicabilityGroup] = Field(default_factory=list, max_length=32)
    topics: list[Label] = Field(default_factory=list, max_length=24)
    tags: list[Label] = Field(default_factory=list, max_length=16)
    issued_at: AwareDatetime | None = None
    schema_pack: SchemaSelection | None = None
    attribute_profile: ProfilePin | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict, max_length=128)

    _check_attributes = field_validator("attributes")(_bounded_attributes)

    @field_validator("localized")
    @classmethod
    def localized_languages(cls, entries: dict[str, LocalizedDocumentMetadata]) -> dict[str, LocalizedDocumentMetadata]:
        normalized = {}
        for language, entry in entries.items():
            tag = normalize_language_tag(language)
            if not tag or tag in normalized or not (entry.title or entry.description):
                raise ValueError("Localized metadata requires distinct language tags and nonempty content")
            normalized[tag] = entry
        return normalized

    @field_validator("subject_categories", "seasons", "topics", "tags")
    @classmethod
    def distinct_labels(cls, labels: list[str]) -> list[str]:
        unique = {}
        for label in labels:
            unique.setdefault(label.casefold(), label)
        return list(unique.values())

    @field_validator("agriculture_subjects")
    @classmethod
    def distinct_subjects(cls, subjects):
        parse_crop_subjects(json.dumps([subject.model_dump(exclude_none=True) for subject in subjects]))
        return subjects

    @field_validator("coverage_areas", "excluded_coverage_areas")
    @classmethod
    def distinct_areas(cls, areas: list[AdministrativeArea]) -> list[AdministrativeArea]:
        if len({area.lookup_key() for area in areas}) != len(areas):
            raise ValueError("Coverage areas must have distinct scheme and area codes")
        return areas

    @field_validator("issued_at", mode="before")
    @classmethod
    def timestamp_string(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("issued_at must be an ISO timestamp with timezone")
        return value

    def storage_value(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True, exclude_defaults=True)


def parse_knowledge_metadata(value: str | None) -> dict | None:
    if value is None:
        return None
    if len(value) > 65_536:
        raise ValueError("Knowledge metadata must be at most 65,536 characters")
    return KnowledgeMetadata.model_validate_json(value).storage_value()


class KnowledgeFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_categories: list[Label] = Field(default_factory=list, max_length=32)
    agriculture_subject_ids: list[str] = Field(default_factory=list, max_length=32)
    coverage_areas: list[AdministrativeArea] = Field(default_factory=list, max_length=32)
    seasons: list[Label] = Field(default_factory=list, max_length=12)
    tags: list[Label] = Field(default_factory=list, max_length=16)

    @field_validator("agriculture_subject_ids")
    @classmethod
    def subject_identifiers(cls, value: list[str]) -> list[str]:
        return parse_crop_subject_ids(value)

    def qdrant_values(self) -> dict[str, list[str]]:
        filters = {
            "subject_categories": self.subject_categories,
            "agriculture_subject_ids": self.agriculture_subject_ids,
            "coverage_area_keys": [area.lookup_key() for area in self.coverage_areas],
            "season_keys": [season_key(season) for season in self.seasons],
            "tag_keys": [tag.casefold() for tag in self.tags],
        }
        return {key: list(dict.fromkeys(values)) for key, values in filters.items() if values}
