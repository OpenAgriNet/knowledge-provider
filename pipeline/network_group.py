from __future__ import annotations

import json
import os
import string
from pathlib import Path
from typing import Optional

from .auth.tenancy import portal_instance
from .network_constants import (
    ADVISORY_PROVIDER_NAME,
    GROUP_PROVIDER_NAME_TEMPLATE,
    PROVIDER_ID_TEMPLATE,
    SCHEMES_PROVIDER_NAME,
)
from .network_provider import PROVIDER_ID_RULE, PROVIDER_KINDS, ProviderNaming, is_valid_provider_id

PROVIDER_CONFIG_OVERRIDE_PATH_ENV = "PROVIDER_CONFIG_OVERRIDE_PATH"
DEFAULT_PROVIDER_CONFIG_OVERRIDE_PATH = Path(__file__).with_name("network_group_providers.json")
PROVIDER_ID_TEMPLATE_ENV = "NETWORK_PROVIDER_ID_TEMPLATE"
GROUP_PROVIDER_NAME_TEMPLATE_ENV = "NETWORK_GROUP_PROVIDER_NAME_TEMPLATE"
ADVISORY_PROVIDER_NAME_ENV = "NETWORK_ADVISORY_PROVIDER_NAME"
SCHEMES_PROVIDER_NAME_ENV = "NETWORK_SCHEMES_PROVIDER_NAME"


class NetworkProviderConfigError(ValueError):
    pass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _check_provider_id(provider_id: str, source: str) -> None:
    if not is_valid_provider_id(provider_id):
        raise NetworkProviderConfigError(f"{source} gives provider id {provider_id!r}; it must be {PROVIDER_ID_RULE}")


def parse_provider_id_overrides(raw: Optional[str], source: str) -> dict[str, dict[str, str]]:
    if raw is None or not raw.strip():
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NetworkProviderConfigError(f"{source} is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise NetworkProviderConfigError(
            f"{source} must be a JSON object of instance code to {{kind: provider id}}"
        )

    overrides: dict[str, dict[str, str]] = {}
    for key, kinds in parsed.items():
        code = key.strip().lower() if isinstance(key, str) else ""
        if not code:
            raise NetworkProviderConfigError(f"{source} entry {key!r} must be a non-empty instance code")
        if code in overrides:
            raise NetworkProviderConfigError(f"{source} names instance {code!r} more than once")
        if not isinstance(kinds, dict) or not kinds:
            raise NetworkProviderConfigError(
                f"{source} entry {key!r} must map knowledge kinds to provider ids"
            )
        provider_ids: dict[str, str] = {}
        for kind, value in kinds.items():
            normalized_kind = kind.strip().lower() if isinstance(kind, str) else ""
            provider_id = value.strip() if isinstance(value, str) else ""
            if normalized_kind not in PROVIDER_KINDS:
                raise NetworkProviderConfigError(
                    f"{source} entry {key!r} kind {kind!r} must be one of {list(PROVIDER_KINDS)}"
                )
            if not provider_id:
                raise NetworkProviderConfigError(
                    f"{source} entry {key!r} kind {kind!r} must have a non-empty provider id"
                )
            _check_provider_id(provider_id, f"{source} entry {key!r} kind {kind!r}")
            provider_ids[normalized_kind] = provider_id
        overrides[code] = provider_ids
    return overrides


def provider_config_override_path() -> Path:
    configured = os.environ.get(PROVIDER_CONFIG_OVERRIDE_PATH_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_PROVIDER_CONFIG_OVERRIDE_PATH


def load_provider_id_overrides() -> dict[str, dict[str, str]]:
    path = provider_config_override_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise NetworkProviderConfigError(f"Provider id overrides {path} cannot be read: {exc}") from exc
    return parse_provider_id_overrides(raw, str(path))


def _checked_template(
    env_name: str, default: str, required: frozenset[str] = frozenset(), **placeholders: str
) -> str:
    template = _env(env_name, default)
    try:
        rendered = template.format(**placeholders)
        used = {field for _, field, _, _ in string.Formatter().parse(template) if field}
    except (KeyError, IndexError, ValueError, AttributeError) as exc:
        raise NetworkProviderConfigError(
            f"{env_name}={template!r} may only use {sorted(placeholders)}: {exc}"
        ) from exc
    if not rendered.strip():
        raise NetworkProviderConfigError(f"{env_name} renders an empty value")
    missing = required - used
    if missing:
        raise NetworkProviderConfigError(f"{env_name}={template!r} must use {sorted(missing)}")
    return template


def _checked_id_template(shared_instance: str) -> str:
    template = _checked_template(
        PROVIDER_ID_TEMPLATE_ENV,
        PROVIDER_ID_TEMPLATE,
        frozenset({"kind", "instance"}),
        kind="advisory",
        instance=shared_instance,
    )
    for kind in PROVIDER_KINDS:
        for instance in (shared_instance, "mh"):
            _check_provider_id(
                template.format(kind=kind, instance=instance), f"{PROVIDER_ID_TEMPLATE_ENV}={template!r}"
            )
    return template


def load_provider_naming() -> ProviderNaming:
    shared_instance = portal_instance()
    return ProviderNaming(
        shared_instance=shared_instance,
        id_template=_checked_id_template(shared_instance),
        group_name_template=_checked_template(
            GROUP_PROVIDER_NAME_TEMPLATE_ENV,
            GROUP_PROVIDER_NAME_TEMPLATE,
            group_name="Maharashtra",
            provider_name="Advisories from documents",
        ),
        provider_names={
            "advisory": _env(ADVISORY_PROVIDER_NAME_ENV, ADVISORY_PROVIDER_NAME),
            "scheme": _env(SCHEMES_PROVIDER_NAME_ENV, SCHEMES_PROVIDER_NAME),
        },
        provider_id_overrides=load_provider_id_overrides(),
    )


def validate_network_provider_config() -> None:
    load_provider_naming()
