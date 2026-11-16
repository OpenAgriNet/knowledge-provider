import json

import pytest

from pipeline.metadata import schema_packs


def _manifest(tmp_path):
    entry = "https://schemas.example.test/current/attributes.yaml"
    base = "https://schemas.example.test/base/attributes.yaml"
    manifest = {
        "capability": "example:Knowledge",
        "version": "v1.0",
        "entrypoint": entry,
        "component": "Knowledge",
        "files": {entry: "entry.json", base: "base.json"},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "entry.json").write_text(json.dumps({
        "components": {"schemas": {"Knowledge": {
            "$ref": "../base/attributes.yaml#/components/schemas/Code",
        }}},
    }))
    return tmp_path / "manifest.json"


def _write_dependency(path, code):
    path.write_text(json.dumps({
        "components": {"schemas": {"Code": {
            "type": "object", "required": ["code"],
            "properties": {"code": {"const": code}},
        }}},
    }))


def test_schema_bundle_requires_all_references_to_be_approved(tmp_path):
    manifest = _manifest(tmp_path)
    (tmp_path / "base.json").write_text(json.dumps({
        "components": {"schemas": {"Code": {"$ref": "https://other.example.test/schema"}}},
    }))
    with pytest.raises(ValueError, match="outside its approved bundle"):
        schema_packs.load_schema_pack(manifest)


@pytest.mark.db
def test_intake_keeps_the_original_pack_after_installing_a_changed_bundle(tmp_path, db_connection):
    manifest = _manifest(tmp_path)
    dependency = tmp_path / "base.json"
    db = db_connection

    _write_dependency(dependency, "original")
    original = schema_packs.load_schema_pack(manifest)
    _write_dependency(dependency, "replacement")
    replacement = schema_packs.load_schema_pack(manifest)
    assert original.digest != replacement.digest

    with db.get_connection() as conn:
        for pack in (original, replacement):
            schema_packs.install_schema_pack(conn, "schema-test.net", pack)

    metadata = {"schema_pack": {
        "network_id": "schema-test.net",
        "capability": original.capability,
        "digest": original.digest,
        "attributes": {"code": "original"},
    }}
    args = {
        "workflow_id": "wf-pinned-pack",
        "document_id": "pack-fixture",
        "filename": "pack.pdf",
        "filepath": "blob://documents/pack.pdf",
        "code": "pack",
        "kind": "advisory",
    }
    db.upsert_document(**args, knowledge_metadata=metadata)
    assert db.get_document("wf-pinned-pack")["knowledge_metadata"] == metadata

    invalid = {"schema_pack": {**metadata["schema_pack"], "digest": replacement.digest}}
    with pytest.raises(ValueError, match="rejected code"):
        db.upsert_document(**{**args, "workflow_id": "wf-invalid", "code": "invalid"}, knowledge_metadata=invalid)
    assert db.get_document("wf-invalid") is None


def test_registry_lookup_ignores_inactive_schemas_and_requires_a_unique_match(monkeypatch):
    import httpx

    from pipeline.network import network_registry
    from pipeline.network.network_connections import NetworkConnection

    active = {
        "capabilityCode": "example:Knowledge", "status": "active", "version": "v1.0",
        "schemaUrl": "https://schemas.example.test/current/attributes.yaml",
    }
    rows = [{**active, "status": "inactive", "version": "v0.9"}, active]

    def lookup(request):
        assert request.url.path == "/api/v1/SchemaRegistry/search"
        assert json.loads(request.content)["filters"]["capabilityCode"] == {"eq": "example:Knowledge"}
        return httpx.Response(200, json={"data": rows})

    client_type = httpx.Client
    monkeypatch.setattr(network_registry.httpx, "Client", lambda **options: client_type(
        transport=httpx.MockTransport(lookup), **options,
    ))
    connection = NetworkConnection(
        "beckn", "test.net", "http://discovery", "provider", "", (), registry_url="http://registry",
    )
    registry = network_registry.SunbirdRegistry(connection)
    assert registry.active_schema("example:Knowledge") == network_registry.RegisteredSchema(
        "example:Knowledge", "v1.0", active["schemaUrl"],
    )
    rows.append(active)
    with pytest.raises(ValueError, match="exactly one active registration"):
        registry.active_schema("example:Knowledge")
