import re
from dataclasses import dataclass, field
from typing import Mapping, Optional

from .network_constants import (
    ADVISORY_CATALOG_NAME,
    GROUP_CATALOG_NAME_TEMPLATE,
    PROVIDER_ID_TEMPLATE,
    SCHEMES_CATALOG_NAME,
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
    shared_name: str
    network_id: Optional[str]
    id_template: str = PROVIDER_ID_TEMPLATE
    group_catalog_name_template: str = GROUP_CATALOG_NAME_TEMPLATE
    catalog_names: Mapping[str, str] = field(
        default_factory=lambda: {
            "advisory": ADVISORY_CATALOG_NAME,
            "scheme": SCHEMES_CATALOG_NAME,
        }
    )
    provider_id_overrides: Mapping[str, str] = field(default_factory=dict)

    def provider(self, group: Optional[Group]) -> dict:
        instance = group.code if group is not None else self.shared_instance
        provider_id = self.provider_id_overrides.get(instance) or self.id_template.format(
            instance=instance, network_id=self.network_id
        )
        if not is_valid_provider_id(provider_id):
            raise InvalidProviderIdError(
                f"Provider id {provider_id!r} for instance {instance!r} must be {PROVIDER_ID_RULE}"
            )
        name = group.name if group is not None else self.shared_name
        return {"id": provider_id, "descriptor": {"code": provider_id, "name": name}}

    def catalog_name(self, kind: str, group: Optional[Group]) -> str:
        name = self.catalog_names[kind]
        if group is None:
            return name
        return self.group_catalog_name_template.format(group_name=group.name, catalog_name=name)
