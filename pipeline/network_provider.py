import re
from dataclasses import dataclass, field
from typing import Mapping, Optional

from .network_constants import (
    ADVISORY_PROVIDER_NAME,
    GROUP_PROVIDER_NAME_TEMPLATE,
    PROVIDER_ID_TEMPLATE,
    SCHEMES_PROVIDER_NAME,
)

PROVIDER_KINDS = ("advisory", "scheme")
PROVIDER_ID_PATTERN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*")
PROVIDER_ID_RULE = "lowercase letters, digits, '-' and '.', with no leading, trailing or repeated '.'"


class InvalidProviderIdError(ValueError):
    pass


def is_valid_provider_id(provider_id: str) -> bool:
    return PROVIDER_ID_PATTERN.fullmatch(provider_id) is not None


@dataclass(frozen=True)
class Group:
    code: str
    name: str


@dataclass(frozen=True)
class ProviderNaming:
    shared_instance: str
    id_template: str = PROVIDER_ID_TEMPLATE
    group_name_template: str = GROUP_PROVIDER_NAME_TEMPLATE
    provider_names: Mapping[str, str] = field(
        default_factory=lambda: {
            "advisory": ADVISORY_PROVIDER_NAME,
            "scheme": SCHEMES_PROVIDER_NAME,
        }
    )
    provider_id_overrides: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def provider(self, kind: str, group: Optional[Group]) -> dict:
        instance = group.code if group is not None else self.shared_instance
        provider_id = self.provider_id_overrides.get(instance, {}).get(kind) or self.id_template.format(
            kind=kind, instance=instance
        )
        if not is_valid_provider_id(provider_id):
            raise InvalidProviderIdError(
                f"Provider id {provider_id!r} for {kind} on instance {instance!r} must be {PROVIDER_ID_RULE}"
            )
        name = self.provider_names[kind]
        if group is not None:
            name = self.group_name_template.format(group_name=group.name, provider_name=name)
        return {"id": provider_id, "descriptor": {"code": provider_id, "name": name}}
