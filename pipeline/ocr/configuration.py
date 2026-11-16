"""Typed, deployment-owned document parser profiles."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .base import OcrConfig
from .extensions import ExtensionParserConfig

PositiveInt = Annotated[int, Field(gt=0)]
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class ParserOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_pages: PositiveInt = 20
    skip_text_layer: bool = True


class EmbeddedTextOptions(ParserOptions):
    type: Literal["pypdf", "mock"]


class LiteParseOptions(ParserOptions):
    type: Literal["liteparse"]
    executable: str = "lit"
    request_timeout_seconds: PositiveSeconds = 300.0


class ChandraOptions(ParserOptions):
    type: Literal["chandra", "chandra_vllm"]
    model: str = "chandra"
    endpoint: str = ""
    api_url: str = ""
    inference_mode: Literal["hf", "vllm"] = "hf"
    max_output_tokens: PositiveInt = 12288
    max_workers: PositiveInt = 4
    image_dpi: PositiveInt = 192
    request_timeout_seconds: PositiveSeconds = 300.0


class MistralOptions(ParserOptions):
    type: Literal["mistral", "mistral_ocr"]
    model: str = "mistral-ocr-latest"
    api_url: str = "https://api.mistral.ai/v1/ocr"
    api_key_file: str
    request_timeout_seconds: PositiveSeconds = 300.0


class ExtensionOptions(ParserOptions):
    type: Literal["extension"]
    plugin: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    options: dict[str, str] = Field(default_factory=dict, max_length=32)


ParserProfile = Annotated[
    EmbeddedTextOptions | LiteParseOptions | ChandraOptions | MistralOptions | ExtensionOptions,
    Field(discriminator="type"),
]
_SINGLE_PROFILE = TypeAdapter(ParserProfile)
_PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class ParserProfiles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: str
    profiles: dict[str, ParserProfile]


def _read_profile(prefix: str) -> ParserProfile:
    values = {
        key.removeprefix(prefix).lower(): value.strip()
        for key, value in os.environ.items()
        if key.startswith(prefix)
    }
    if values.get("type") == "extension":
        values["options"] = {
            key.removeprefix("option_"): values.pop(key) for key in tuple(values) if key.startswith("option_")
        }
    try:
        return _SINGLE_PROFILE.validate_python(values)
    except ValidationError as error:
        # Validation errors can include the entire input. Report only the error kind.
        error_type = error.errors(include_input=False)[0]["type"]
        raise ValueError(f"Invalid {prefix}* settings ({error_type})") from None


def read_profiles() -> ParserProfiles | None:
    names_value = os.environ.get("DOCUMENT_PARSER_PROFILES")
    if names_value is None:
        if not any(key.startswith("DOCUMENT_PARSER_") for key in os.environ):
            return None
        profile = _read_profile("DOCUMENT_PARSER_")
        return ParserProfiles(default=profile.type, profiles={profile.type: profile})

    names = [name.strip() for name in names_value.split(",")]
    if len(set(names)) != len(names) or any(not _PROFILE_NAME.fullmatch(name) for name in names):
        raise ValueError("DOCUMENT_PARSER_PROFILES must list unique lowercase identifiers")
    # Underscores keep profile prefixes readable. Reject overlapping prefixes.
    if any(left != right and right.startswith(f"{left}_") for left in names for right in names):
        raise ValueError("DOCUMENT_PARSER_PROFILES names must have distinct prefixes")
    if "DOCUMENT_PARSER_TYPE" in os.environ:
        raise ValueError("Set DOCUMENT_PARSER_TYPE or DOCUMENT_PARSER_PROFILES")
    default = os.environ.get("DOCUMENT_PARSER_DEFAULT", names[0]).strip()
    if default not in names:
        raise ValueError("DOCUMENT_PARSER_DEFAULT must name a configured profile")
    profiles = {name: _read_profile(f"DOCUMENT_PARSER_{name.upper()}_") for name in names}
    return ParserProfiles(default=default, profiles=profiles)


def resolve_profile(config: ParserProfiles, name: str | None) -> OcrConfig:
    selected = name or config.default
    profile = config.profiles.get(selected)
    if profile is None:
        raise ValueError("Requested document parser profile is not configured")

    options = profile.model_dump(exclude={"type", "api_key_file"})
    if isinstance(profile, MistralOptions):
        try:
            options["api_key"] = Path(profile.api_key_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise ValueError("Unable to read the Mistral parser credential file") from None
        if not options["api_key"]:
            raise ValueError("The Mistral parser credential file is empty")
    config_type = ExtensionParserConfig if isinstance(profile, ExtensionOptions) else OcrConfig
    return config_type(provider=profile.type, **options)
