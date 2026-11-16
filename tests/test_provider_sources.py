import json

import pytest

from pipeline.intake import provider_sources


def test_settings_validate_templates_and_schedule_without_network():
    update = provider_sources.SourceSettingsUpdate(version=0, templates={
        "notices": {"fields": {"external_id": "/id", "title": "/title", "text": "/text"}},
    }, crawl={"notices": {"host": "notices.example", "intake_template": "notices"}}, schedules=[{
        "source_key": "daily", "host": "notices.example", "url": "https://notices.example/feed",
        "interval_seconds": 900, "intake_template": "notices",
    }])
    validated = provider_sources.validate_settings("example", update, {})
    assert validated["schedules"][0]["intake_template"] == "notices"
    assert "mapping" not in validated["schedules"][0]
    assert validated["templates"]["notices"]["runtime"] == "json-pointer-v1"


def test_provider_settings_cannot_select_or_retarget_credentials():
    prior = {"crawl": {"feed": {"host": "notices.example", "authorization_env": "CRAWL_AUTH_FIXTURE"}}}
    same = provider_sources.SourceSettingsUpdate(version=1, crawl={"feed": {"host": "notices.example", "authenticated": True}})
    validated = provider_sources.validate_settings("example", same, prior)
    assert validated["crawl"]["feed"]["authorization_env"] == "CRAWL_AUTH_FIXTURE"
    public = provider_sources.public_settings({"version": 2, "settings": validated})
    assert public["crawl"]["feed"]["authenticated"] is True
    assert "CRAWL_AUTH" not in json.dumps(public)
    with pytest.raises(ValueError, match="another host"):
        provider_sources.validate_settings("example", same.model_copy(update={"crawl": {"feed": {"host": "other.example"}}}), prior)
    with pytest.raises(ValueError, match="deployment operator"):
        provider_sources.validate_settings("example", same.model_copy(update={"crawl": {"new": {"host": "notices.example", "authorization_env": "CRAWL_AUTH_FIXTURE"}}}), prior)


def test_scheduled_settings_require_the_saved_https_host():
    update = provider_sources.SourceSettingsUpdate(version=0, schedules=[{
        "source_key": "daily", "host": "notices.example", "url": "https://other.example/feed",
    }])
    with pytest.raises(ValueError, match="saved source host"):
        provider_sources.validate_settings("example", update, {})


@pytest.mark.db
def test_provider_settings_versioning_and_template_isolation(db_connection, monkeypatch):
    from pipeline.intake.scheduled_intake import configured_profiles
    from pipeline.intake.webhook_mapping import load_intake_template

    monkeypatch.delenv("SCHEDULED_PULL_PROFILES_FILE", raising=False)
    monkeypatch.delenv("CRAWL_SOURCE_PROFILES", raising=False)
    monkeypatch.delenv("KNOWLEDGE_INTAKE_TEMPLATES_FILE", raising=False)
    db_connection.get_or_create_instance("example")
    db_connection.get_or_create_instance("other")
    update = provider_sources.SourceSettingsUpdate(version=0, templates={"notices": {"runtime": "canonical-v1"}}, schedules=[{
        "source_key": "daily", "host": "notices.example", "url": "https://notices.example/feed", "intake_template": "notices",
    }])
    saved = provider_sources.save_settings("example", update, "fixture-admin")
    assert saved["version"] == 1
    assert configured_profiles()[0].provider == "example"
    assert load_intake_template("notices", provider="example").runtime == "canonical-v1"
    with pytest.raises(ValueError, match="No intake templates"):
        load_intake_template("notices", provider="other")
    with pytest.raises(FileExistsError):
        provider_sources.save_settings("example", update, "fixture-admin")
