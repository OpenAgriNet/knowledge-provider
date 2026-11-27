"""Optional loader for tests of the workspace migration kit."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


def migration_kit_path():
    path = Path(__file__).resolve().parents[3] / "bh-prod-data" / "migrations"
    if not path.is_dir():
        pytest.skip("The workspace migration kit is not present")
    return path


def load_migration_tool(name):
    path = migration_kit_path() / f"{name}.py"
    if not path.is_file():
        pytest.skip(f"The workspace migration tool {name} is not present")
    spec = spec_from_file_location(name, path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
