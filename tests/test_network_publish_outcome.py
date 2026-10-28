import json

import pytest

from pipeline.api.api import _network_publish_outcome


def artifact(artifact_type, metadata=None, created_at="2026-10-28T10:00:00"):
    return {
        "artifact_type": artifact_type,
        "metadata_json": json.dumps(metadata) if metadata is not None else None,
        "created_at": created_at,
    }


@pytest.mark.unit
def test_no_publish_artifact_means_no_outcome():
    assert _network_publish_outcome([artifact("ocr_output")]) is None


@pytest.mark.unit
def test_disabled_publish_is_skipped_with_its_reason():
    outcome = _network_publish_outcome([
        artifact("network_publish_skipped", {"reason_code": "disabled", "reason": "Publish to Network is disabled."}),
    ])

    assert outcome.status == "skipped"
    assert outcome.reason_code == "disabled"
    assert outcome.reason == "Publish to Network is disabled."


@pytest.mark.unit
def test_a_skip_recorded_before_reason_codes_reads_as_an_unmapped_kind():
    outcome = _network_publish_outcome([artifact("network_publish_skipped", {"skipped": True})])

    assert outcome.status == "skipped"
    assert outcome.reason_code == "unmapped_kind"
    assert outcome.reason


@pytest.mark.unit
@pytest.mark.parametrize("result_status", ["ACCEPTED", "PARTIAL", None])
def test_a_sent_payload_is_published(result_status):
    outcome = _network_publish_outcome([artifact("network_publish_payload", {"result_status": result_status})])

    assert outcome.status == "published"
    assert outcome.reason is None


@pytest.mark.unit
def test_a_rejected_payload_carries_the_errors():
    outcome = _network_publish_outcome([
        artifact("network_publish_payload", {"result_status": "REJECTED", "errors": ["bad provider"]}),
    ])

    assert outcome.status == "rejected"
    assert "bad provider" in outcome.reason


@pytest.mark.unit
def test_the_latest_publish_artifact_wins():
    outcome = _network_publish_outcome([
        artifact("network_publish_payload", {"result_status": "ACCEPTED"}, "2026-10-28T11:00:00"),
        artifact("network_publish_skipped", {"reason_code": "disabled", "reason": "off"}, "2026-10-28T10:00:00"),
    ])

    assert outcome.status == "published"
