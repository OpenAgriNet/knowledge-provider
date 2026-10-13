"""
Group and Group Provider resolution for Publish to Network.

A Group is the state a document belongs to, named by its state code and taken
from the document's `instance`. The portal instance (Bharat Vistaar) and the
default instance have no Group: their documents keep the Shared Provider. See
docs/ADR/0006-publish-to-network-under-per-group-provider.md.

New features say Group; existing screens, roles and data still say state or
instance, and this module is the one place that maps between them.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from .instances import PORTAL_INSTANCE

# JSON object of group code -> Group Provider id, e.g. {"mh": "mh.provider.oan.dev"}.
GROUP_PROVIDER_IDS_ENV = "NETWORK_GROUP_PROVIDER_IDS"


class GroupProviderConfigError(ValueError):
    """The Group Provider map is malformed; the API and worker refuse to boot."""


def _no_group_instances() -> set[str]:
    # Follow the configured values, so a deployment that renames its portal or
    # default instance never mistakes it for a state.
    portal = (os.environ.get("PORTAL_INSTANCE") or PORTAL_INSTANCE).strip().lower()
    default = (os.environ.get("DEFAULT_INSTANCE") or "default").strip().lower()
    return {portal or PORTAL_INSTANCE, default or "default"}


def group_for_instance(instance: Optional[str]) -> Optional[str]:
    """The document's group code, or None when its instance has no Group."""
    code = (instance or "").strip().lower()
    if not code or code in _no_group_instances():
        return None
    return code


def parse_group_provider_ids(raw: Optional[str]) -> dict[str, str]:
    """Parse the Group Provider map; unset or blank means no Group has a provider.

    Keys are lowercased so `MH` and `mh` name the same Group. Anything other
    than an object of non-empty strings raises, rather than silently skipping
    every Group's publish.
    """
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
    """The Group Provider map from the environment; raises if malformed."""
    return parse_group_provider_ids(os.environ.get(GROUP_PROVIDER_IDS_ENV))
