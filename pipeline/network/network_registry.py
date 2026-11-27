"""Read schema, participant, and route records from a configured network Registry."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic
from typing import Literal, Protocol
from urllib.parse import unquote, urlsplit

import httpx

from pipeline.network.network_connections import NetworkConnection, network_connection

_MAX_RESPONSE_BYTES = 128 * 1024
_ACTIONS = frozenset({"discover", "select", "init", "confirm", "status", "track", "cancel", "update", "rate", "support"})
_PARTICIPANT_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{2,252}")
_CAPABILITY_CODE = re.compile(r"openagrinet:[A-Z][A-Za-z0-9]*")
_MAPPING_RUNTIME = re.compile(
    r"[A-Za-z0-9._-]+@(?:v?[0-9]+(?:\.[0-9]+){1,2}(?:[-+][A-Za-z0-9.+-]+)?|sha256:[0-9a-f]{64})"
)

Failure = Literal["missing", "duplicate", "inactive", "invalid", "mismatch", "unavailable"]


class RegistryLookupError(ValueError):
    """A Registry rejection or failure, with a stable classification."""

    def __init__(self, entity: str, reason: Failure):
        super().__init__(f"Registry {entity} lookup: {reason}")
        self.entity = entity
        self.reason = reason


@dataclass(frozen=True)
class RegisteredSchema:
    capability: str
    version: str
    artifact_url: str


@dataclass(frozen=True)
class ProviderAdmission:
    network_id: str
    registry_url: str
    participant_id: str
    record_id: str
    updated_at: str | None
    base_url: str


@dataclass(frozen=True)
class ParticipantObservation:
    record_id: str
    revision: str
    name: str
    base_url: str
    status: str


@dataclass(frozen=True)
class ActionObservation:
    action: str
    method: str
    path: str
    mapping_url: str
    timeout_ms: int
    retry_max: int
    status: str


@dataclass(frozen=True)
class BindingObservation:
    record_id: str
    revision: str
    status: str
    actions: tuple[ActionObservation, ...]


@dataclass(frozen=True)
class ApprovedRoute:
    """An operator-approved route and locally verified mapping pin."""

    network_id: str
    participant_id: str
    capability: str
    action: str
    base_url: str
    method: str
    path: str
    mapping_url: str
    mapping_sha256: str
    mapping_runtime: str
    auth_profile: str
    timeout_ms: int = 15_000
    retry_max: int = 0

    def __post_init__(self) -> None:
        if (not self.network_id or not _PARTICIPANT_ID.fullmatch(self.participant_id)
                or len(self.participant_id) > 64 or not _CAPABILITY_CODE.fullmatch(self.capability)
                or self.action not in _ACTIONS):
            raise ValueError("Invalid approved route identity")
        if not _valid_url(self.base_url) or not _valid_action(
            self.action, self.method, self.path, self.mapping_url, self.timeout_ms, self.retry_max,
        ):
            raise ValueError("Invalid approved route destination or action")
        if (not re.fullmatch(r"[0-9a-f]{64}", self.mapping_sha256)
                or not _MAPPING_RUNTIME.fullmatch(self.mapping_runtime) or not self.auth_profile.strip()):
            raise ValueError("An approved route requires a mapping digest, runtime pin, and auth profile")

    @property
    def binding_key(self) -> str:
        return f"{self.participant_id}|{self.capability}"


@dataclass(frozen=True)
class RouteBindingEvidence:
    """An immutable observation of an active binding matching its local approval."""

    admission: ProviderAdmission
    approval: ApprovedRoute
    binding_record_id: str
    binding_updated_at: str | None
    action_record_id: str | None

    @property
    def digest(self) -> str:
        serialized = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()


class NetworkRegistry(Protocol):
    def active_schema(self, capability: str) -> RegisteredSchema: ...

    def active_provider(self, participant_id: str) -> ProviderAdmission: ...

    def observe_participant(self, participant_id: str) -> ParticipantObservation | None: ...

    def observe_binding(self, participant_id: str, capability: str) -> BindingObservation | None: ...

    def ready_route(self, approval: ApprovedRoute) -> RouteBindingEvidence: ...


def _valid_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 2000:
        return False
    if any(char in value for char in "?#\\") or any(char.isspace() or ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.username is None


def _valid_action(
    action: str, method: object, path: object, mapping_url: object, timeout_ms: object, retry_max: object,
) -> bool:
    return (
        method in ("GET", "POST")
        and isinstance(path, str)
        and bool(re.fullmatch(r"/[A-Za-z0-9._~%/-]{0,511}", path))
        and "//" not in path
        and not {".", ".."}.intersection(unquote(path).split("/"))
        and isinstance(mapping_url, str)
        and _valid_url(mapping_url)
        and mapping_url.endswith((f".{action}.yaml", f".{action}.yml"))
        and type(timeout_ms) is int and 1_000 <= timeout_ms <= 120_000
        and type(retry_max) is int and 0 <= retry_max <= 5
    )


def _active_record(rows: list[dict], entity: str) -> dict:
    if not rows:
        raise RegistryLookupError(entity, "missing")
    if len(rows) > 1:
        raise RegistryLookupError(entity, "duplicate")
    status = rows[0].get("status")
    if not isinstance(status, str) or not status:
        raise RegistryLookupError(entity, "invalid")
    if status.casefold() != "active":
        raise RegistryLookupError(entity, "inactive")
    return rows[0]


def _record_string(row: dict, field: str, entity: str, *, required: bool = False) -> str | None:
    value = row.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RegistryLookupError(entity, "invalid")
    return value


def _only_record(rows: list[dict], entity: str) -> dict | None:
    if len(rows) > 1:
        raise RegistryLookupError(entity, "duplicate")
    return rows[0] if rows else None


def _observed_revision(row: dict, entity: str) -> str:
    return _record_string(row, "osUpdatedAt", entity) or _record_string(row, "osCreatedAt", entity, required=True)


def _observed_status(row: dict, entity: str) -> str:
    status = _record_string(row, "status", entity, required=True).casefold()
    if status not in {"active", "inactive"}:
        raise RegistryLookupError(entity, "invalid")
    return status


class SunbirdRegistry:
    def __init__(self, connection: NetworkConnection):
        if not _valid_url(connection.registry_url):
            raise ValueError("The selected network requires a valid Registry URL")
        self._connection = connection
        self._base = connection.registry_url.rstrip("/")
        if not self._base.endswith("/api/v1"):
            self._base += "/api/v1"

    def _headers(self) -> dict[str, str]:
        if not self._connection.registry_token_file:
            return {}
        if not self._base.startswith("https://"):
            raise ValueError("An authenticated Registry requires HTTPS")
        try:
            with Path(self._connection.registry_token_file).open(encoding="utf-8") as token_file:
                raw = token_file.read(8193)
        except (OSError, UnicodeError):
            raise RegistryLookupError("credential", "unavailable") from None
        token = raw.strip()
        if not token or len(raw) > 8192 or any(character.isspace() for character in token):
            raise ValueError("The Registry credential file is invalid")
        return {"Authorization": f"Bearer {token}"}

    def _search(
        self, entity: str, field: str, value: str, *, active_only: bool = False,
    ) -> list[dict]:
        filters = {field: {"eq": value}}
        if active_only:
            filters["status"] = {"eq": "active"}
        headers = {"Accept-Encoding": "identity", **self._headers()}
        deadline = monotonic() + 10
        try:
            with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
                with client.stream(
                    "POST", f"{self._base}/{entity}/search", headers=headers, json={"filters": filters},
                ) as response:
                    response.raise_for_status()
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise RegistryLookupError(entity, "invalid")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if monotonic() >= deadline:
                            raise RegistryLookupError(entity, "unavailable")
                        if len(data) + len(chunk) > _MAX_RESPONSE_BYTES:
                            raise RegistryLookupError(entity, "invalid")
                        data.extend(chunk)
        except httpx.HTTPError:
            raise RegistryLookupError(entity, "unavailable") from None
        try:
            payload = json.loads(data)
        except (ValueError, RecursionError):
            raise RegistryLookupError(entity, "invalid") from None
        if isinstance(payload, dict):
            params = payload.get("params", {})
            if not isinstance(params, dict):
                raise RegistryLookupError(entity, "invalid")
            status = params.get("status")
            if status is not None and (not isinstance(status, str) or status.casefold() not in {"success", "successful"}):
                raise RegistryLookupError(entity, "unavailable")
            payload = payload.get("data")
        if (not isinstance(payload, list)
                or any(not isinstance(row, dict) or row.get(field) != value for row in payload)):
            raise RegistryLookupError(entity, "invalid")
        return payload

    def active_schema(self, capability: str) -> RegisteredSchema:
        rows = self._search("SchemaRegistry", "capabilityCode", capability, active_only=True)
        rows = [row for row in rows if row.get("status") == "active"]
        if len(rows) != 1:
            raise ValueError("The Registry must return exactly one active registration for the capability")
        version, url = rows[0].get("version"), rows[0].get("schemaUrl")
        if not isinstance(version, str) or not _valid_url(url) or not url.startswith("https://"):
            raise ValueError("The Registry returned an invalid schema version or artifact URL")
        return RegisteredSchema(capability, version, url)

    def active_provider(self, participant_id: str) -> ProviderAdmission:
        if not _PARTICIPANT_ID.fullmatch(participant_id):
            raise ValueError("Invalid Registry participant ID")
        row = _active_record(self._search("Participant", "participantId", participant_id), "Participant")
        if row.get("type") != "upstream":
            raise RegistryLookupError("Participant", "mismatch")
        base_url = row.get("baseUrl")
        if not _valid_url(base_url):
            raise RegistryLookupError("Participant", "invalid")
        return ProviderAdmission(
            self._connection.network_id, self._base, participant_id,
            _record_string(row, "osid", "Participant", required=True),
            _record_string(row, "osUpdatedAt", "Participant"), base_url,
        )

    def observe_participant(self, participant_id: str) -> ParticipantObservation | None:
        if not _PARTICIPANT_ID.fullmatch(participant_id):
            raise ValueError("Invalid Registry participant ID")
        entity = "Participant"
        row = _only_record(self._search(entity, "participantId", participant_id), entity)
        if row is None:
            return None
        if row.get("type") != "upstream" or not _valid_url(row.get("baseUrl")):
            raise RegistryLookupError(entity, "mismatch")
        return ParticipantObservation(
            _record_string(row, "osid", entity, required=True), _observed_revision(row, entity),
            _record_string(row, "name", entity, required=True), row["baseUrl"], _observed_status(row, entity),
        )

    def observe_binding(self, participant_id: str, capability: str) -> BindingObservation | None:
        if (not _PARTICIPANT_ID.fullmatch(participant_id) or len(participant_id) > 64
                or not _CAPABILITY_CODE.fullmatch(capability)):
            raise ValueError("Invalid Registry binding identity")
        entity = "ProviderSchema"
        binding = _only_record(self._search(entity, "bindingKey", f"{participant_id}|{capability}"), entity)
        if binding is None:
            return None
        if (binding.get("participantId"), binding.get("capabilityCode")) != (participant_id, capability):
            raise RegistryLookupError(entity, "mismatch")
        actions = binding.get("actions")
        if not isinstance(actions, list) or not 1 <= len(actions) <= 10:
            raise RegistryLookupError("ActionBinding", "invalid")
        observed = []
        seen = set()
        for row in actions:
            if not isinstance(row, dict):
                raise RegistryLookupError("ActionBinding", "invalid")
            action = row.get("action")
            fields = (
                row.get("method"), row.get("path"), row.get("mappings"),
                row.get("timeoutMs", 15_000), row.get("retryMax", 0),
            )
            if not isinstance(action, str) or action not in _ACTIONS or not _valid_action(action, *fields):
                raise RegistryLookupError("ActionBinding", "invalid")
            if action in seen:
                raise RegistryLookupError("ActionBinding", "duplicate")
            seen.add(action)
            observed.append(ActionObservation(action, *fields, _observed_status(row, "ActionBinding")))
        return BindingObservation(
            _record_string(binding, "osid", entity, required=True), _observed_revision(binding, entity),
            _observed_status(binding, entity), tuple(observed),
        )

    def ready_route(self, approval: ApprovedRoute) -> RouteBindingEvidence:
        """Compare the Registry to an approval. Never call the route or fetch the mapping."""
        if approval.network_id != self._connection.network_id:
            raise RegistryLookupError("network", "mismatch")
        admission = self.active_provider(approval.participant_id)
        if admission.base_url != approval.base_url:
            raise RegistryLookupError("Participant", "mismatch")

        rows = self._search("ProviderSchema", "bindingKey", approval.binding_key)
        binding = _active_record(rows, "ProviderSchema")
        if (binding.get("participantId"), binding.get("capabilityCode")) != (
            approval.participant_id, approval.capability,
        ):
            raise RegistryLookupError("ProviderSchema", "mismatch")
        actions = binding.get("actions")
        if not isinstance(actions, list) or any(not isinstance(row, dict) for row in actions):
            raise RegistryLookupError("ActionBinding", "invalid")
        action = _active_record([row for row in actions if row.get("action") == approval.action], "ActionBinding")
        registered_action = (
            action.get("method"), action.get("path"), action.get("mappings"),
            action.get("timeoutMs", 15_000), action.get("retryMax", 0),
        )
        if not _valid_action(approval.action, *registered_action):
            raise RegistryLookupError("ActionBinding", "invalid")
        if registered_action != (
            approval.method, approval.path, approval.mapping_url, approval.timeout_ms, approval.retry_max,
        ):
            raise RegistryLookupError("ActionBinding", "mismatch")
        return RouteBindingEvidence(
            admission, approval,
            _record_string(binding, "osid", "ProviderSchema", required=True),
            _record_string(binding, "osUpdatedAt", "ProviderSchema"),
            _record_string(action, "osid", "ActionBinding"),
        )


def registry_for_network(network_id: str) -> NetworkRegistry:
    connection = network_connection(network_id)
    if connection.adapter != "beckn":
        raise ValueError("The configured network adapter has no Registry integration")
    return SunbirdRegistry(connection)
