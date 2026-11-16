from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from pipeline import db, vector_store
from pipeline.api.routers import providers, search
from pipeline.auth import deps, keycloak_admin
from pipeline.auth.deps import CurrentUser
from pipeline.auth.jwt import claims_to_user
from pipeline.auth.permissions import Permission
from pipeline.vector_store import qdrant_store


class KeycloakGroups:
    """Test double at the Keycloak HTTP boundary, with independent memberships."""

    def __init__(self):
        self.groups = {}
        self.users = {}
        self.memberships = {}
        self._add_group("/global/super-admin")
        for user in ("platform", "owner", "member"):
            self.users[user] = {
                "id": user, "email": f"{user}@example.test", "username": user,
                "emailVerified": True, "enabled": True,
            }
            self.memberships[user] = set()
        self.memberships["platform"].add(self.groups["/global/super-admin"]["id"])

    def _add_group(self, path):
        if path in self.groups:
            raise HTTPException(409)
        self.groups[path] = {"id": f"group-{len(self.groups)}", "path": path, "name": path.rsplit("/", 1)[1]}

    def _by_id(self, group_id):
        return next(group for group in self.groups.values() if group["id"] == group_id)

    def request(self, method, url, *, token, body=None):
        assert token == "test-admin-token"
        request = urlsplit(url)
        path = request.path.removeprefix("/admin/realms/local/")
        query = parse_qs(request.query)
        if path == "users":
            email = query["email"][0]
            return 200, [user for user in self.users.values() if user["email"] == email]
        if path.startswith("users/"):
            _, user, *rest = path.split("/")
            if user not in self.users:
                raise HTTPException(404)
            if not rest:
                return 200, self.users[user]
            if method == "GET":
                return 200, [self._by_id(group_id) for group_id in sorted(self.memberships[user])]
            if method == "PUT":
                self.memberships[user].add(rest[1])
            elif method == "DELETE":
                self.memberships[user].remove(rest[1])
            return 204, None
        if path.startswith("group-by-path/"):
            group = self.groups.get("/" + path.removeprefix("group-by-path/"))
            if group is None:
                raise HTTPException(404)
            return 200, group
        if path == "groups":
            parent = ""
        else:
            _, group_id, *rest = path.split("/")
            group = self._by_id(group_id)
            parent = group["path"]
            if not rest:
                return 200, group
            if rest == ["members"]:
                return 200, [self.users[user] for user, groups in self.memberships.items() if group_id in groups]
        if method == "POST":
            self._add_group(f"{parent}/{body['name']}")
            return 201, None
        return 200, [group for key, group in self.groups.items() if key.rsplit("/", 1)[0] == parent]


def test_provider_lifecycle_and_revocation_with_same_user_token(monkeypatch):
    keycloak = KeycloakGroups()
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("KEYCLOAK_VERIFY_LIVE_GROUPS", "true")
    monkeypatch.setattr(keycloak_admin, "require_admin_config", lambda: SimpleNamespace(base_url="http://keycloak", realm="local"))
    monkeypatch.setattr(keycloak_admin, "_admin_token", lambda _: "test-admin-token")
    monkeypatch.setattr(keycloak_admin, "_req", keycloak.request)
    monkeypatch.setattr(deps, "decode_and_validate_token", lambda token, _: claims_to_user({
        "sub": token, "groups": ["/states/RICE-LAB/admin", "/states/COTTON-HUB/admin"],
        "realm_access": {"roles": ["state_admin"]}, "instances": ["rice-lab", "cotton-hub"],
    }))
    monkeypatch.setattr(deps, "_sync_user_row", lambda _: None)
    connection = SimpleNamespace(execute=lambda *args: None)
    monkeypatch.setattr(db, "get_connection", lambda: nullcontext(connection))
    instances = {}
    monkeypatch.setattr(db, "upsert_instance", lambda path, keycloak_group_id: instances.setdefault(path, keycloak_group_id))
    monkeypatch.setattr(db, "list_provider_instances", lambda scope: [
        {"provider": path.rsplit("/", 1)[1].lower(), "id": group_id}
        for path, group_id in instances.items() if scope is None or path.rsplit("/", 1)[1].lower() in scope
    ])
    monkeypatch.setattr(db, "get_search_settings", lambda: {})
    monkeypatch.setattr(qdrant_store, "get_vector_size", lambda: 2)
    monkeypatch.setattr(qdrant_store, "embed_passages", lambda texts, **_: [[1.0, 1.0] for _ in texts])
    monkeypatch.setattr(qdrant_store, "embed_query", lambda *args, **_: [1.0, 1.0])
    store = qdrant_store.QdrantVectorStore(client=QdrantClient(":memory:"))
    store.upsert("test-providers", [
        {"doc_id": code, "instance": code, "text": "cotton advisory", "is_reference": False}
        for code in ("rice-lab", "cotton-hub", "outside")
    ])
    monkeypatch.setattr(vector_store, "get_vector_store", lambda: store)

    app = FastAPI()
    app.include_router(providers.router)
    app.include_router(search.router)

    @app.get("/access")
    def access(user: CurrentUser):
        return {provider: user.has_permission_for_instance(Permission.UPLOAD, provider) for provider in user.instances}

    def headers(user):
        return {"Authorization": f"Bearer {user}"}

    with TestClient(app) as client:
        for code in ("rice-lab", "cotton-hub", "rice-lab"):
            response = client.put(f"/providers/{code}", headers=headers("platform"), json={
                "admin_email": "owner@example.test" if code == "rice-lab" else "platform@example.test",
            })
            assert response.status_code == 200, response.text
        assert len(instances) == 2
        assert [row["provider"] for row in client.get("/providers", headers=headers("owner")).json()["providers"]] == ["rice-lab"]
        contributor = {"email": "member@example.test", "role": "state_contributor"}
        assert client.post("/providers/cotton-hub/members", headers=headers("owner"), json=contributor).status_code == 403
        assert client.post("/providers/rice-lab/members", headers=headers("owner"), json=contributor).status_code == 200
        assert client.post("/providers/cotton-hub/members", headers=headers("platform"), json={
            **contributor, "role": "state_view",
        }).status_code == 200
        assert client.get("/access", headers=headers("member")).json() == {"cotton-hub": False, "rice-lab": True}
        members = client.get("/providers/rice-lab/members", headers=headers("owner")).json()["members"]
        assert {row["user_id"]: row["role"] for row in members} == {"member": "state_contributor", "owner": "state_admin"}

        query = {"index_name": "test-providers", "query": "cotton"}
        hits = client.post("/search", headers=headers("member"), json=query).json()["hits"]
        assert {row["instance"] for row in hits} == {"cotton-hub", "rice-lab"}
        assert client.delete("/providers/rice-lab/members/owner", headers=headers("owner")).status_code == 403
        assert client.delete("/providers/rice-lab/members/member", headers=headers("owner")).status_code == 200
        assert client.get("/access", headers=headers("member")).json() == {"cotton-hub": False}
        assert client.post("/search", headers=headers("member"), json={**query, "group": "rice-lab"}).status_code == 403
        hits = client.post("/search", headers=headers("member"), json=query).json()["hits"]
        assert {row["instance"] for row in hits} == {"cotton-hub"}


def test_intake_lists_all_registered_providers_only_for_platform_administrators(monkeypatch):
    from pipeline.api import api

    monkeypatch.setenv("KEYCLOAK_PROVIDER_GROUP_ROOT", "providers")
    monkeypatch.delenv("PORTAL_INSTANCE", raising=False)
    reads = []

    def registered(scope):
        reads.append(scope)
        return [{"provider": "rice-lab"}, {"provider": "cotton-hub"}]

    monkeypatch.setattr(db, "list_provider_instances", registered)
    client = TestClient(api.app)

    def list_as(groups):
        user = claims_to_user({"sub": "member", "groups": groups})
        monkeypatch.setitem(api.app.dependency_overrides, deps.get_current_user, lambda: user)
        return client.get("/upload/providers")

    admin = list_as(["/global/super-admin"])
    assert admin.status_code == 200
    assert admin.json() == {"providers": ["cotton-hub", "rice-lab"], "default_provider": None}

    contributor = list_as([
        "/global/global-viewer", "/providers/RICE-LAB/contributor", "/providers/COTTON-HUB/view",
    ])
    assert contributor.status_code == 200
    assert contributor.json() == {"providers": ["rice-lab"], "default_provider": None}
    assert list_as(["/global/global-viewer"]).status_code == 403
    assert reads == [None]


def test_readable_provider_choices_match_document_access(monkeypatch):
    from pipeline.api import api

    monkeypatch.setenv("KEYCLOAK_PROVIDER_GROUP_ROOT", "providers")
    monkeypatch.setattr(db, "list_provider_instances", lambda _: [{"provider": "registered"}])
    client = TestClient(api.app)

    def choices_for(groups):
        user = claims_to_user({"sub": "member", "groups": groups})
        monkeypatch.setitem(api.app.dependency_overrides, deps.get_current_user, lambda: user)
        return client.get("/providers/readable").json()["providers"]

    assert choices_for(["/global/super-admin"]) == ["registered"]
    assert choices_for(["/global/global-viewer"]) == ["registered"]
    assert choices_for(["/providers/MEMBER/view"]) == ["member"]
    assert choices_for([]) == []


def test_document_actions_use_permission_in_the_document_provider(monkeypatch):
    from pipeline.api import api

    actor = claims_to_user({"sub": "member", "groups": ["/states/RICE/admin", "/states/COTTON/view"]})
    monkeypatch.setitem(api.app.dependency_overrides, deps.get_current_user, lambda: actor)
    docs = {
        "rice-test": {"workflow_id": "rice-test", "instance": "rice", "stage": "ocr_review"},
        "cotton-test": {"workflow_id": "cotton-test", "instance": "cotton", "stage": "ocr_review"},
    }
    monkeypatch.setattr(db, "get_document", docs.get)

    @asynccontextmanager
    async def no_services(_app):
        yield

    monkeypatch.setattr(api.app.router, "lifespan_context", no_services)
    with TestClient(api.app, raise_server_exceptions=True) as client:
        denied = [
            client.patch("/documents/cotton-test/pages/1", json={"edited_markdown": "Changed"}),
            client.patch("/documents/cotton-test/chunks/1", json={"edited_text": "Changed"}),
            client.post("/documents/cotton-test/retry-ocr"),
            client.post("/documents/cotton-test/approve-ocr"),
            client.post("/documents/cotton-test/approve-ingestion"),
        ]
        assert [response.status_code for response in denied] == [403] * len(denied)
        bulk = client.post("/documents/bulk/approve-ocr", json={
            "workflow_ids": ["rice-test", "cotton-test"], "dry_run": True,
        }).json()
        assert {item["workflow_id"]: item["ok"] for item in bulk["results"]} == {"rice-test": True, "cotton-test": False}
