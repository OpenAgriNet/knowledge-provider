"""Editable pilot metadata and the fields a reviewer can override."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pipeline.documents import document_validity
from pipeline.metadata.extraction import Label
from pipeline.metadata.knowledge_metadata import AdministrativeArea

MetadataField = Literal["category", "crop", "season", "location"]
METADATA_FIELDS = ("category", "crop", "season", "location")


class ExistingEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=600)


class EditableFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: MetadataField
    value: Label
    area: AdministrativeArea | None = None
    rule: int | None = Field(None, ge=1, le=256)
    evidence: ExistingEvidence | None = None

    @model_validator(mode="after")
    def check_scope(self):
        if self.area is not None and self.field != "location":
            raise ValueError("An area code belongs to a location")
        if self.rule is not None and self.field not in {"location", "season"}:
            raise ValueError("An applicability group belongs to a location or season")
        return self


class MetadataEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=r"^[a-f0-9]{64}$")
    overrides: list[MetadataField] = Field(max_length=4)
    validity: document_validity.TimePeriod | None = None
    facts: list[EditableFact] = Field(max_length=256)

    @model_validator(mode="after")
    def check_overrides(self):
        if any(fact.field not in self.overrides for fact in self.facts):
            raise ValueError("Enable the field override before saving values")
        if len(set(self.overrides)) != len(self.overrides):
            raise ValueError("Duplicate field override")
        return self


def supplied_facts(doc: dict) -> list[dict]:
    metadata = doc.get("knowledge_metadata") or {}
    facts = [
        {"field": "category", "value": category}
        for category in metadata.get("subject_categories", [])
    ]
    if doc.get("kind") == "scheme" and not any(fact["value"] == "Scheme" for fact in facts):
        facts.append({"field": "category", "value": "Scheme"})
    typed_crops = [subject for subject in metadata.get("subjects", []) if subject.get("type") == "crop"]
    legacy_crops = metadata.get("agriculture_subjects", []) if metadata.get("subject_categories") == ["Crop"] else []
    for reference in [*(doc.get("crop_subjects") or []), *typed_crops, *legacy_crops]:
        if name := (reference.get("descriptor") or {}).get("name"):
            fact = {"field": "crop", "value": name}
            if fact not in facts:
                facts.append(fact)
    if any(fact["field"] == "crop" for fact in facts) and not any(fact["value"] == "Crop" for fact in facts):
        facts.append({"field": "category", "value": "Crop"})
    groups = metadata.get("applicability_groups") or [metadata]
    for rule, group in enumerate(groups, start=1):
        facts.extend({"field": "season", "value": season, "rule": rule} for season in group.get("seasons", []))
        for area in group.get("coverage_areas", []):
            facts.append({"field": "location", "value": area.get("areaName") or area["areaCode"], "area": area, "rule": rule})
    return facts



def document_review_facts(doc: dict, review: dict) -> list[dict]:
    accepted = review.get("accepted") or {}
    overrides = accepted.get("overrides", [])
    fallback = [fact for fact in supplied_facts(doc) if fact["field"] not in overrides]
    return list(accepted.get("facts", [])) + fallback
