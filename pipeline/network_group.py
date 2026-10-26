from __future__ import annotations

import json
import os
import string
from pathlib import Path
from typing import Optional

from .auth.tenancy import portal_instance
from .catalog_builder import normalize_document_kind
from .instances import INSTANCE_NAMES, instance_display_name
from .network_constants import (
    ADVISORY_CATALOG_NAME,
    GROUP_CATALOG_NAME_TEMPLATE,
    PROVIDER_ID_TEMPLATE,
    SCHEMES_CATALOG_NAME,
)
from .network_provider import (
    PROVIDER_ID_RULE,
    PROVIDER_KINDS,
    Group,
    ProviderNaming,
    is_valid_provider_id,
)

PROVIDER_CONFIG_OVERRIDE_PATH_ENV = "PROVIDER_CONFIG_OVERRIDE_PATH"
DEFAULT_PROVIDER_CONFIG_OVERRIDE_PATH = Path(__file__).with_name("network_group_providers.json")
PROVIDER_ID_TEMPLATE_ENV = "NETWORK_PROVIDER_ID_TEMPLATE"
GROUP_CATALOG_NAME_TEMPLATE_ENV = "NETWORK_GROUP_CATALOG_NAME_TEMPLATE"
ADVISORY_CATALOG_NAME_ENV = "NETWORK_ADVISORY_CATALOG_NAME"
SCHEMES_CATALOG_NAME_ENV = "NETWORK_SCHEMES_CATALOG_NAME"
NETWORK_ID_ENV = "NETWORK_SENDER_ID"
SAMPLE_NETWORK_ID = "provider.oan.dev"


class NetworkProviderConfigError(ValueError):
    pass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _check_provider_id(provider_id: str, source: str) -> None:
    if not is_valid_provider_id(provider_id):
        raise NetworkProviderConfigError(f"{source} gives provider id {provider_id!r}; it must be {PROVIDER_ID_RULE}")


def parse_provider_id_overrides(raw: Optional[str], source: str) -> dict[str, str]:
    if raw is None or not raw.strip():
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NetworkProviderConfigError(f"{source} is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise NetworkProviderConfigError(f"{source} must be a JSON object of instance code to provider id")

    overrides: dict[str, str] = {}
    for key, value in parsed.items():
        code = key.strip().lower() if isinstance(key, str) else ""
        if not code:
            raise NetworkProviderConfigError(f"{source} entry {key!r} must be a non-empty instance code")
        if code in overrides:
            raise NetworkProviderConfigError(f"{source} names instance {code!r} more than once")
        if not isinstance(value, str) or not value.strip():
            raise NetworkProviderConfigError(
                f"{source} entry {key!r} must map to a provider id string, e.g. {{\"{code}\": \"{code}.example.com\"}}"
            )
        provider_id = value.strip()
        _check_provider_id(provider_id, f"{source} entry {key!r}")
        overrides[code] = provider_id
    return overrides


def provider_config_override_path() -> Path:
    configured = os.environ.get(PROVIDER_CONFIG_OVERRIDE_PATH_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_PROVIDER_CONFIG_OVERRIDE_PATH


def load_provider_id_overrides() -> dict[str, str]:
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


def _checked_id_template(shared_instance: str, network_id: Optional[str]) -> str:
    sample_network_id = network_id or SAMPLE_NETWORK_ID
    template = _checked_template(
        PROVIDER_ID_TEMPLATE_ENV,
        PROVIDER_ID_TEMPLATE,
        frozenset({"instance", "network_id"}),
        instance=shared_instance,
        network_id=sample_network_id,
    )
    for instance in dict.fromkeys((shared_instance, *INSTANCE_NAMES)):
        _check_provider_id(
            template.format(instance=instance, network_id=sample_network_id),
            f"{PROVIDER_ID_TEMPLATE_ENV}={template!r} with {NETWORK_ID_ENV}={sample_network_id!r}",
        )
    return template


def load_provider_naming(network_id: Optional[str] = None) -> ProviderNaming:
    shared_instance = portal_instance()
    network_id = (network_id or os.environ.get(NETWORK_ID_ENV) or "").strip() or None
    return ProviderNaming(
        shared_instance=shared_instance,
        shared_name=instance_display_name(shared_instance),
        network_id=network_id,
        id_template=_checked_id_template(shared_instance, network_id),
        group_catalog_name_template=_checked_template(
            GROUP_CATALOG_NAME_TEMPLATE_ENV,
            GROUP_CATALOG_NAME_TEMPLATE,
            frozenset({"group_name"}),
            group_name="Maharashtra",
            catalog_name="Advisories from documents",
        ),
        catalog_names={
            "advisory": _env(ADVISORY_CATALOG_NAME_ENV, ADVISORY_CATALOG_NAME),
            "scheme": _env(SCHEMES_CATALOG_NAME_ENV, SCHEMES_CATALOG_NAME),
        },
        provider_id_overrides=load_provider_id_overrides(),
    )


def validate_network_provider_config() -> None:
    load_provider_naming()


def named_group(code: Optional[str]) -> Optional[Group]:
    if code is None:
        return None
    return Group(code=code, name=instance_display_name(code))


def provider_for(naming: ProviderNaming, document_kind: Optional[str], group: Optional[Group]) -> Optional[dict]:
    if normalize_document_kind(document_kind) not in PROVIDER_KINDS or naming.network_id is None:
        return None
    return naming.provider(group)
