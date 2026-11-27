"""Contributor-supplied document crop context using agriculture subject references."""

from typing import Annotated

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)


class CropDescriptor(BaseModel):
    """Supported identification fields from the Beckn Descriptor."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str | None = Field(default=None, min_length=1, max_length=256)
    name: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def identified(self) -> "CropDescriptor":
        if not self.code and not self.name:
            raise ValueError("A crop descriptor needs a code or name")
        return self


class AgricultureSubjectReference(BaseModel):
    """AgricultureResource v0.1 reference; does not assert taxonomy verification."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    subjectId: str | None = Field(default=None, min_length=1, max_length=2048)
    descriptor: CropDescriptor | None = None

    @field_validator("subjectId")
    @classmethod
    def absolute_subject_uri(cls, value: str | None) -> str | None:
        if value is not None:
            if "\\" in value or any(character.isspace() for character in value):
                raise ValueError("subjectId must be an absolute URI without spaces or backslashes")
            TypeAdapter(AnyUrl).validate_python(value)
        return value

    @model_validator(mode="after")
    def identified(self) -> "AgricultureSubjectReference":
        if not self.subjectId and self.descriptor is None:
            raise ValueError("A crop subject needs subjectId or descriptor")
        return self


CropSubjects = Annotated[list[AgricultureSubjectReference], Field(max_length=32)]


def parse_crop_subjects(raw: str | None) -> list[dict] | None:
    """Distinguish omitted metadata from an explicitly empty crop list."""
    if raw is None:
        return None
    if len(raw) > 32768:
        raise ValueError("crop_subjects must be at most 32768 characters")
    subjects = TypeAdapter(CropSubjects).validate_json(raw)
    result = [subject.model_dump(exclude_none=True) for subject in subjects]
    if len({subject.model_dump_json(exclude_none=True) for subject in subjects}) != len(result):
        raise ValueError("Crop subject references must be unique")
    return result


def parse_crop_subject_ids(value: object) -> list[str]:
    """Validate the bounded OR-list used to filter explicit crop identifiers."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("crop_subject_ids must be a list of at most 32 absolute URI strings")
    identifiers = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("Each crop_subject_ids entry must be an absolute URI string")
        reference = AgricultureSubjectReference(subjectId=item)
        if reference.subjectId not in identifiers:
            identifiers.append(reference.subjectId)
    return identifiers
