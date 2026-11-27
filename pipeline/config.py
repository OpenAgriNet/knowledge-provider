"""Check application configuration without displaying configuration values."""

from dataclasses import dataclass

from .object_storage import ObjectStoreConfig, load_object_store_config


@dataclass(frozen=True)
class Config:
    object_store: ObjectStoreConfig


class ConfigurationError(Exception):
    """An integration has invalid configuration."""


def load_config() -> Config:
    try:
        return Config(object_store=load_object_store_config())
    except (RuntimeError, ValueError) as error:
        raise ConfigurationError(str(error)) from None


def validate_environment() -> list[str]:
    try:
        load_config()
    except ConfigurationError as error:
        return [str(error)]
    return []


def print_config_status() -> None:
    errors = validate_environment()
    if errors:
        print("Object storage configuration is invalid.")
    else:
        print("Object storage configuration is valid.")


if __name__ == "__main__":
    print_config_status()
