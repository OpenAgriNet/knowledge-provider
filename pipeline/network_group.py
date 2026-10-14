from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .instances import PORTAL_INSTANCE

GROUP_PROVIDERS_CONFIG_PATH_ENV = "NETWORK_GROUP_PROVIDERS_CONFIG_PATH"
DEFAULT_GROUP_PROVIDERS_CONFIG_PATH = Path(__file__).with_name("network_group_providers.json")


class GroupProviderConfigError(ValueError):
    pass


def _no_group_instances() -> set[str]:
    portal = (os.environ.get("PORTAL_INSTANCE") or PORTAL_INSTANCE).strip().lower()
    default = (os.environ.get("DEFAULT_INSTANCE") or "default").strip().lower()
    return {portal or PORTAL_INSTANCE, default or "default"}


def group_for_instance(instance: Optional[str]) -> Optional[str]:
    code = (instance or "").strip().lower()
    if not code or code in _no_group_instances():
        return None
    return code


def parse_group_provider_ids(raw: Optional[str], source: str) -> dict[str, str]:
    if raw is None or not raw.strip():
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GroupProviderConfigError(f"{source} is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise GroupProviderConfigError(
            f"{source} must be a JSON object of group code to provider id"
        )

    provider_ids: dict[str, str] = {}
    for key, value in parsed.items():
        code = key.strip().lower() if isinstance(key, str) else ""
        provider_id = value.strip() if isinstance(value, str) else ""
        if not code or not provider_id:
            raise GroupProviderConfigError(
                f"{source} entry {key!r}: {value!r} must map a group code "
                "to a non-empty provider id"
            )
        if code in provider_ids:
            raise GroupProviderConfigError(
                f"{source} names group {code!r} more than once"
            )
        provider_ids[code] = provider_id
    return provider_ids


def group_providers_config_path() -> Path:
    configured = os.environ.get(GROUP_PROVIDERS_CONFIG_PATH_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_GROUP_PROVIDERS_CONFIG_PATH


def load_group_provider_ids() -> dict[str, str]:
    path = group_providers_config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GroupProviderConfigError(f"Group provider config {path} cannot be read: {exc}") from exc
    return parse_group_provider_ids(raw, str(path))
