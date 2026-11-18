"""Deployment-owned schemas for the domain attributes collected at intake."""

import hashlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .schema_packs import _encoded, _load_file, _references

ProfileID = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]


class ProfilePin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: ProfileID
    version: str = Field(min_length=1, max_length=40)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class IntakeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: ProfileID
    version: str = Field(min_length=1, max_length=40)
    kind: ProfileID
    subject_category: str | None = Field(default=None, min_length=1, max_length=128)
    schema_definition: dict = Field(alias="schema")

    def pin(self) -> ProfilePin:
        digest = hashlib.sha256(_encoded(self.model_dump())).hexdigest()
        return ProfilePin(id=self.id, version=self.version, digest=digest)

    def form(self) -> dict:
        return {
            "pin": self.pin().model_dump(),
            "kind": self.kind,
            "subject_category": self.subject_category,
            "properties": self.schema_definition.get("properties", {}),
            "required": self.schema_definition.get("required", []),
            "additional_properties": self.schema_definition.get("additionalProperties", True) is not False,
        }


def load_intake_profiles() -> list[IntakeProfile]:
    configured = os.environ.get("INTAKE_METADATA_PROFILES")
    return list(_configured_profiles(configured)) if configured else []


@lru_cache(maxsize=8)
def _configured_profiles(configured: str) -> tuple[IntakeProfile, ...]:
    path = Path(configured).resolve()
    try:
        manifest = _load_file(path.parent, path.name)
        if set(manifest) != {"profiles"} or not isinstance(manifest["profiles"], list) or len(manifest["profiles"]) > 32:
            raise ValueError("Expected a list of at most 32 intake profiles")
        profiles = [IntakeProfile.model_validate(entry) for entry in manifest["profiles"]]
        selectors = {(profile.kind, (profile.subject_category or "").casefold()) for profile in profiles}
        if len(selectors) != len(profiles) or len({profile.id for profile in profiles}) != len(profiles):
            raise ValueError("Intake profile IDs and selectors must be distinct")
        for profile in profiles:
            schema = profile.schema_definition
            if schema.get("type") != "object" or any(True for _ in _references(schema)):
                raise ValueError("Intake profiles must use self-contained object schemas")
            Draft202012Validator.check_schema(schema)
    except (OSError, ValueError, SchemaError, yaml.YAMLError) as exc:
        raise IntakeProfileConfigurationError("Intake metadata profiles are misconfigured") from exc
    return tuple(profiles)


class IntakeProfileValidationError(ValueError):
    """Submitted attributes do not match the selected metadata profile."""


class IntakeProfileConfigurationError(RuntimeError):
    """An operator must fix the configured profile file."""


def apply_intake_profile(kind: str, metadata: dict | None) -> dict | None:
    """Resolve, validate and pin metadata before accepting a new intake."""
    from pipeline.metadata.schema_packs import validate_subject_category

    values = metadata or {}
    for category in values.get("subject_categories", []):
        validate_subject_category(category)
    pin = values.get("attribute_profile")
    categories = {category.casefold() for category in values.get("subject_categories", [])}
    default = None
    matched = []
    for profile in load_intake_profiles():
        if profile.kind != kind:
            continue
        if profile.subject_category is None:
            default = profile
        elif profile.subject_category.casefold() in categories:
            matched.append(profile)
    if len(matched) > 1:
        raise IntakeProfileValidationError("Choose one subject category with a metadata profile")
    selected = matched[0] if matched else default
    if not selected:
        if pin:
            raise IntakeProfileValidationError("The metadata profile changed. Reload the form.")
        return metadata
    current_pin = selected.pin().model_dump()
    if pin and pin != current_pin:
        raise IntakeProfileValidationError("The metadata profile changed. Reload the form.")
    validator = Draft202012Validator(selected.schema_definition, format_checker=Draft202012Validator.FORMAT_CHECKER)
    try:
        validator.validate(values.get("attributes", {}))
    except ValidationError as exc:
        field = ".".join(map(str, exc.absolute_path)) or "Attributes"
        if exc.validator == "required":
            missing = [key for key in exc.validator_value if key not in exc.instance]
            raise IntakeProfileValidationError(f"Required attributes: {', '.join(missing)}") from None
        raise IntakeProfileValidationError(f"{field} failed the {exc.validator} rule") from None
    return {**values, "attribute_profile": current_pin}
