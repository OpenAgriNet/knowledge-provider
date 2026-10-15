from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .catalog_builder import ProviderNaming
from .instances import PORTAL_INSTANCE
from .network_constants import (
    ADVISORY_PROVIDER_NAME,
    GROUP_PROVIDER_NAME_TEMPLATE,
    PROVIDER_ID_TEMPLATE,
    SCHEMES_PROVIDER_NAME,
)

GROUP_PROVIDERS_CONFIG_PATH_ENV = "NETWORK_GROUP_PROVIDERS_CONFIG_PATH"
DEFAULT_GROUP_PROVIDERS_CONFIG_PATH = Path(__file__).with_name("network_group_providers.json")
PROVIDER_ID_TEMPLATE_ENV = "NETWORK_PROVIDER_ID_TEMPLATE"
GROUP_PROVIDER_NAME_TEMPLATE_ENV = "NETWORK_GROUP_PROVIDER_NAME_TEMPLATE"
ADVISORY_PROVIDER_NAME_ENV = "NETWORK_ADVISORY_PROVIDER_NAME"
SCHEMES_PROVIDER_NAME_ENV = "NETWORK_SCHEMES_PROVIDER_NAME"


class NetworkProviderConfigError(ValueError):
    pass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def portal_instance() -> str:
    return _env("PORTAL_INSTANCE", PORTAL_INSTANCE).lower()


def _no_group_instances() -> set[str]:
    return {portal_instance(), _env("DEFAULT_INSTANCE", "default").lower()}


def group_for_instance(instance: Optional[str]) -> Optional[str]:
    code = (instance or "").strip().lower()
    if not code or code in _no_group_instances():
        return None
    return code


def parse_group_codes(raw: Optional[str], source: str) -> frozenset[str]:
    if raw is None or not raw.strip():
        return frozenset()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NetworkProviderConfigError(f"{source} is not valid JSON: {exc}") from exc

    if not isinstance(parsed, list):
        raise NetworkProviderConfigError(f"{source} must be a JSON array of group codes")

    codes: set[str] = set()
    for entry in parsed:
        code = entry.strip().lower() if isinstance(entry, str) else ""
        if not code:
            raise NetworkProviderConfigError(f"{source} entry {entry!r} must be a non-empty group code")
        if code in codes:
            raise NetworkProviderConfigError(f"{source} names group {code!r} more than once")
        codes.add(code)
    return frozenset(codes)


def group_providers_config_path() -> Path:
    configured = os.environ.get(GROUP_PROVIDERS_CONFIG_PATH_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_GROUP_PROVIDERS_CONFIG_PATH


def load_group_codes() -> frozenset[str]:
    path = group_providers_config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise NetworkProviderConfigError(f"Group provider config {path} cannot be read: {exc}") from exc
    return parse_group_codes(raw, str(path))


def _checked_template(env_name: str, default: str, **placeholders: str) -> str:
    template = _env(env_name, default)
    try:
        rendered = template.format(**placeholders)
    except (KeyError, IndexError, ValueError) as exc:
        raise NetworkProviderConfigError(
            f"{env_name}={template!r} may only use {sorted(placeholders)}: {exc}"
        ) from exc
    if not rendered.strip():
        raise NetworkProviderConfigError(f"{env_name} renders an empty value")
    return template


def load_provider_naming() -> ProviderNaming:
    return ProviderNaming(
        shared_instance=portal_instance(),
        id_template=_checked_template(
            PROVIDER_ID_TEMPLATE_ENV, PROVIDER_ID_TEMPLATE, kind="advisory", instance="bv"
        ),
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
    )


def validate_network_provider_config() -> None:
    load_group_codes()
    load_provider_naming()
