import pytest

from pipeline.network.network_constants import (
    KIND_TO_CATALOG_TYPE,
    catalog_type_for_kind,
)


@pytest.mark.parametrize("kind", ["scheme", "advisory", "video"])
def test_every_kind_resolves_to_its_catalog_type(kind):
    assert catalog_type_for_kind(kind) == KIND_TO_CATALOG_TYPE[kind]


def test_multiple_kinds_can_share_the_same_schema():
    assert catalog_type_for_kind("scheme") == catalog_type_for_kind("advisory")


def test_video_is_a_known_kind():
    assert catalog_type_for_kind(" Video ") == "openagrinet:VideoResource"


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        catalog_type_for_kind("custom")
