"""Provider-owned source settings with deployment bootstrap fallback."""

import json
import os
import re

import httpx
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from pipeline import db

_CODE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class CrawlProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    host: str = Field(pattern=r"^[a-zA-Z0-9.-]{1,253}$")
    parser_profile: str | None = Field(default=None, max_length=64)
    intake_template: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    authorization_env: str | None = Field(default=None, pattern=r"^CRAWL_AUTH_[A-Z0-9_]{1,100}$")


class SourceSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=0)
    release_network_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")
    templates: dict = Field(default_factory=dict, max_length=64)
    crawl: dict = Field(default_factory=dict, max_length=32)
    schedules: list[dict] = Field(default_factory=list, max_length=32)


def stored_settings(provider: str) -> dict | None:
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT s.settings, s.version FROM provider_source_settings s "
            "JOIN instances i ON i.id = s.provider_id WHERE lower(i.name) = %s", (provider,),
        ).fetchone()


def all_stored_settings() -> dict[str, dict]:
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT lower(i.name) AS provider, s.settings FROM provider_source_settings s "
            "JOIN instances i ON i.id = s.provider_id",
        ).fetchall()
    return {row["provider"]: row["settings"] for row in rows}


def bootstrap_settings(provider: str) -> dict:
    from pipeline.intake.scheduled_intake import bootstrap_profiles

    crawl = json.loads(os.environ.get("CRAWL_SOURCE_PROFILES") or "{}").get(provider, {})
    schedules = [
        profile.model_dump(mode="json", exclude={"provider", "intake_template"})
        for profile in bootstrap_profiles() if profile.provider == provider
    ]
    return {"templates": {}, "crawl": crawl, "schedules": schedules}


def source_settings(provider: str) -> dict:
    row = stored_settings(provider)
    return row if row else {"version": 0, "settings": bootstrap_settings(provider)}


def public_settings(row: dict) -> dict:
    # Return access mode, never a credential reference or value.
    settings = json.loads(json.dumps(row["settings"]))
    for profile in [*settings["crawl"].values(), *settings["schedules"]]:
        profile["authenticated"] = bool(profile.pop("authorization_env", None))
    return {"version": row["version"], **settings}


def validate_settings(provider: str, update: SourceSettingsUpdate, previous: dict) -> dict:
    from pipeline.intake.scheduled_intake import PullProfile
    from pipeline.intake.webhook_mapping import SOURCE_MAPPING, load_intake_template

    if len(update.model_dump_json().encode()) > 262144:
        raise ValueError("Source settings must be at most 256 KiB")
    if any(not _CODE.fullmatch(key) for key in [*update.templates, *update.crawl]):
        raise ValueError("Template and source names must use lowercase letters, numbers and hyphens")
    templates = {key: SOURCE_MAPPING.validate_python(value).model_dump(mode="json") for key, value in update.templates.items()}

    def template(name):
        if name in templates:
            return templates[name]
        # Deployment templates remain available as bootstrap fallback.
        return load_intake_template(name).model_dump(mode="json")

    def bind_access(candidate, old):
        candidate = dict(candidate)
        authenticated = candidate.pop("authenticated", False)
        if candidate.get("authorization_env"):
            raise ValueError("Credential references must be provisioned by the deployment operator")
        candidate.pop("authorization_env", None)
        if old and old.get("authorization_env"):
            if candidate.get("host", "").lower() != old["host"].lower():
                raise ValueError("An authenticated source cannot be moved to another host")
            candidate["authorization_env"] = old["authorization_env"]
        elif authenticated:
            raise ValueError("Ask the deployment operator to provision access for this source")
        return candidate

    crawl = {}
    for key, value in update.crawl.items():
        item = CrawlProfile.model_validate(bind_access(value, previous.get("crawl", {}).get(key)))
        if item.intake_template:
            template(item.intake_template)
        crawl[key] = item.model_dump(mode="json", exclude_none=True)
    schedules = []
    previous_schedules = {item["source_key"]: item for item in previous.get("schedules", [])}
    for value in update.schedules:
        if "provider" in value:
            raise ValueError("Scheduled sources belong to the selected provider")
        candidate = bind_access(value, previous_schedules.get(value.get("source_key")))
        template_name = candidate.pop("intake_template", None)
        if template_name:
            if "mapping" in candidate:
                raise ValueError("Choose an intake template or supply a mapping")
            candidate["mapping"] = template(template_name)
        item = PullProfile.model_validate({**candidate, "provider": provider})
        url = httpx.URL(item.url)
        if (url.scheme != "https" or url.port not in (None, 443) or url.userinfo or url.fragment
                or url.host.lower() != item.host.lower()
                or any(ch.isspace() or ord(ch) < 32 for ch in item.url)):
            raise ValueError("Scheduled URLs must use HTTPS on the saved source host")
        stored = item.model_dump(mode="json", exclude={"provider", "intake_template"}, exclude_none=True)
        if template_name:
            stored.pop("mapping", None)
            stored["intake_template"] = template_name
        schedules.append(stored)
    if len({item["source_key"] for item in schedules}) != len(schedules):
        raise ValueError("Scheduled source keys must be unique")
    release_network_id = update.release_network_id
    if release_network_id:
        from pipeline.network.network_connections import configured_network_connections

        if not any(c.network_id == release_network_id for c in configured_network_connections()):
            raise ValueError("Choose a configured publication network")
    return {"templates": templates, "crawl": crawl, "schedules": schedules, "release_network_id": release_network_id}


def save_settings(provider: str, update: SourceSettingsUpdate, actor: str) -> dict:
    with db.get_connection() as conn:
        row = conn.execute("SELECT id FROM instances WHERE lower(name) = %s FOR UPDATE", (provider,)).fetchone()
        if not row:
            raise LookupError("Provider not found")
        current = conn.execute(
            "SELECT settings, version FROM provider_source_settings WHERE provider_id = %s", (row["id"],),
        ).fetchone()
        version = current["version"] if current else 0
        if version != update.version:
            raise FileExistsError("Source settings changed. Reload before saving.")
        settings = validate_settings(provider, update, current["settings"] if current else bootstrap_settings(provider))
        conn.execute(
            "INSERT INTO provider_source_settings(provider_id, settings, version, updated_by) VALUES (%s, %s, 1, %s) "
            "ON CONFLICT(provider_id) DO UPDATE SET settings = excluded.settings, "
            "version = provider_source_settings.version + 1, updated_by = excluded.updated_by, updated_at = now()",
            (row["id"], Jsonb(settings), actor),
        )
    return public_settings({"version": version + 1, "settings": settings})
