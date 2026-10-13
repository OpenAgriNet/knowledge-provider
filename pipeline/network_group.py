from __future__ import annotations

import json
import os
from typing import Optional

from .instances import PORTAL_INSTANCE

GROUP_PROVIDER_IDS_ENV = "NETWORK_GROUP_PROVIDER_IDS"


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


def parse_group_provider_ids(raw: Optional[str]) -> dict[str, str]:
    if raw is None or not raw.strip():
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GroupProviderConfigError(f"{GROUP_PROVIDER_IDS_ENV} is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise GroupProviderConfigError(
            f"{GROUP_PROVIDER_IDS_ENV} must be a JSON object of group code to provider id"
        )

    provider_ids: dict[str, str] = {}
    for key, value in parsed.items():
        code = key.strip().lower() if isinstance(key, str) else ""
        provider_id = value.strip() if isinstance(value, str) else ""
        if not code or not provider_id:
            raise GroupProviderConfigError(
                f"{GROUP_PROVIDER_IDS_ENV} entry {key!r}: {value!r} must map a group code "
                "to a non-empty provider id"
            )
        if code in provider_ids:
            raise GroupProviderConfigError(
                f"{GROUP_PROVIDER_IDS_ENV} names group {code!r} more than once"
            )
        provider_ids[code] = provider_id
    return provider_ids


def load_group_provider_ids() -> dict[str, str]:
    return parse_group_provider_ids(os.environ.get(GROUP_PROVIDER_IDS_ENV))
