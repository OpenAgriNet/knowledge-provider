"""Snapshot of every HTTP route on the FastAPI app.

Safety net for splitting pipeline/api into routers. If you intentionally add,
remove or rename a route, update this list in the same change.
"""

import pytest
from starlette.routing import BaseRoute

from tests.route_inventory import application_routes, iter_api_routes

EXPECTED_ROUTES: list[tuple[str, str]] = [
    ("DELETE", "/documents/{workflow_id}"),
    ("DELETE", "/documents/{workflow_id}/releases/{network_id}"),
    ("DELETE", "/providers/{provider}/members/{user_id}"),
    ("GET", "/admin/access-options"),
    ("GET", "/admin/index/schema"),
    ("GET", "/admin/ingest-info"),
    ("GET", "/admin/users"),
    ("GET", "/audit"),
    ("GET", "/auth/me"),
    ("GET", "/auth/google"),
    ("GET", "/auth/google/callback"),
    ("POST", "/auth/google/start"),
    ("POST", "/auth/google/complete"),
    ("GET", "/catalog/v1/schemes"),
    ("GET", "/catalog/v1/snapshot"),
    ("GET", "/catalog/v1/tool-prompt"),
    ("GET", "/catalog/v1/version"),
    ("GET", "/chunks/search"),
    ("GET", "/documents"),
    ("GET", "/documents/cohorts"),
    ("GET", "/documents/summary"),
    ("GET", "/documents/{workflow_id}"),
    ("GET", "/documents/{workflow_id}/allowed-actions"),
    ("GET", "/documents/{workflow_id}/artifacts"),
    ("GET", "/documents/{workflow_id}/artifacts/{artifact_id}"),
    ("GET", "/documents/{workflow_id}/artifacts/{artifact_id}/content"),
    ("GET", "/documents/{workflow_id}/audit"),
    ("GET", "/documents/{workflow_id}/chunks"),
    ("GET", "/documents/{workflow_id}/chunks/{chunk_num}"),
    ("GET", "/documents/{workflow_id}/error-details"),
    ("GET", "/documents/{workflow_id}/export/chunks"),
    ("GET", "/documents/{workflow_id}/export/markdown"),
    ("GET", "/documents/{workflow_id}/graph"),
    ("GET", "/documents/{workflow_id}/jobs"),
    ("GET", "/documents/{workflow_id}/pages"),
    ("GET", "/documents/{workflow_id}/pages/{page_num}"),
    ("GET", "/documents/{workflow_id}/pdf"),
    ("GET", "/documents/{workflow_id}/qdrant"),
    ("GET", "/documents/{workflow_id}/qdrant/chunks"),
    ("GET", "/documents/{workflow_id}/revisions"),
    ("GET", "/documents/{workflow_id}/revisions/{revision_id}"),
    ("GET", "/documents/{workflow_id}/runtime"),
    ("GET", "/documents/{workflow_id}/stage-io"),
    ("GET", "/health"),
    ("GET", "/indexes/summary"),
    ("GET", "/indexes/{index_name}/settings"),
    ("GET", "/indexes/{index_name}/stats"),
    ("GET", "/networks/{network_id}/registry-proposals"),
    ("GET", "/networks/{network_id}/registry-proposals/{proposal_id}"),
    ("POST", "/providers/{provider}/networks/{network_id}/registry-proposals"),
    ("PUT", "/networks/{network_id}/registry-proposals/{proposal_id}/decision"),
    ("GET", "/operations/queue"),
    ("GET", "/pipeline/stages"),
    ("GET", "/provenance/chunk"),
    ("GET", "/providers"),
    ("GET", "/providers/readable"),
    ("GET", "/providers/{provider}/members"),
    ("GET", "/providers/{provider}/networks/{network_id}/registry-context"),
    ("GET", "/providers/{provider}/registry-networks"),
    ("GET", "/providers/{provider}/scheduled-sources"),
    ("GET", "/runs"),
    ("GET", "/runs/{job_id}"),
    ("GET", "/search/collections"),
    ("GET", "/providers/{provider}/source-settings"),
    ("PUT", "/providers/{provider}/source-settings"),
    ("GET", "/providers/{provider}/crawl"),
    ("GET", "/knowledge/intake/{provider}"),
    ("GET", "/knowledge/intake/{provider}/{source}/deliveries"),
    ("GET", "/knowledge/intake/{provider}/{source}/deliveries/original"),
    ("POST", "/knowledge/intake/{provider}/mapping-preview"),
    ("GET", "/settings/search"),
    ("GET", "/settings/search/audit"),
    ("GET", "/upload/options"),
    ("GET", "/upload/providers"),
    ("GET", "/upload/scheme-matches"),
    ("GET", "/knowledge/intake/schema"),
    ("GET", "/knowledge/intake/{provider}/{source}"),
    ("PUT", "/knowledge/intake/{provider}/{source}"),
    ("POST", "/knowledge/intake/{provider}/{source}/submissions"),
    ("GET", "/webhook/sources/{provider}/{source}"),
    ("PATCH", "/documents/{workflow_id}/chunks/{chunk_num}"),
    ("PATCH", "/documents/{workflow_id}/pages/{page_num}"),
    ("PATCH", "/documents/{workflow_id}/scheme-metadata"),
    ("POST", "/admin/index/create"),
    ("POST", "/admin/users"),
    ("POST", "/auth/otp/refresh"),
    ("POST", "/auth/session/refresh"),
    ("POST", "/auth/email/send"),
    ("POST", "/auth/email/verify"),
    ("POST", "/auth/session/logout"),
    ("POST", "/catalog/v1/rebuild"),
    ("POST", "/documents"),
    ("POST", "/documents/bulk/approve-chunks"),
    ("POST", "/documents/bulk/approve-ocr"),
    ("POST", "/documents/bulk/approve-translation"),
    ("POST", "/documents/bulk/reindex"),
    ("POST", "/documents/reconcile"),
    ("POST", "/documents/{workflow_id}/approve-chunks"),
    ("POST", "/documents/{workflow_id}/approve-ingestion"),
    ("POST", "/documents/{workflow_id}/approve-ocr"),
    ("POST", "/documents/{workflow_id}/approve-prod"),
    ("POST", "/documents/{workflow_id}/approve-translation"),
    ("POST", "/documents/{workflow_id}/chunks/{chunk_num}/reset"),
    ("POST", "/documents/{workflow_id}/clear-reindex-required"),
    ("POST", "/documents/{workflow_id}/mark-reindex-required"),
    ("POST", "/documents/{workflow_id}/pages/{page_num}/reset"),
    ("POST", "/documents/{workflow_id}/reconcile"),
    ("POST", "/documents/{workflow_id}/reingest"),
    ("POST", "/documents/{workflow_id}/request-prod-ready"),
    ("POST", "/documents/{workflow_id}/restore"),
    ("POST", "/documents/{workflow_id}/retry-chunking"),
    ("POST", "/documents/{workflow_id}/retry-ingestion"),
    ("POST", "/documents/{workflow_id}/retry-ocr"),
    ("POST", "/documents/{workflow_id}/retry-translation"),
    ("POST", "/providers/{provider}/crawl"),
    ("POST", "/providers/{provider}/crawl/{source_id}"),
    ("POST", "/knowledge/intake/{provider}/{source}/token"),
    ("POST", "/providers/{provider}/members"),
    ("POST", "/search"),
    ("POST", "/search/"),
    ("POST", "/settings/search/reset"),
    ("POST", "/upload"),
    ("POST", "/upload/batch"),
    ("POST", "/upload/{workflow_id}/start"),
    ("POST", "/webhook/sources/{provider}/{source}/events"),
    ("PUT", "/admin/users/{user_id}/access"),
    ("PUT", "/documents/{workflow_id}/releases/{network_id}"),
    ("PUT", "/providers/{provider}"),
    ("PUT", "/providers/{provider}/members/{user_id}"),
    ("PUT", "/settings/search"),
    ("PUT", "/webhook/sources/{provider}/{source}"),
]

def collect_routes(app) -> list[tuple[str, str]]:
    """Return sorted (METHOD, path) pairs for every application route on ``app``."""
    return sorted({(method, path) for method, path, _ in application_routes(app)})


@pytest.mark.api
@pytest.mark.unit
def test_route_snapshot_matches_app():
    from pipeline.api import app

    assert collect_routes(app) == sorted(EXPECTED_ROUTES)


@pytest.mark.unit
def test_inventory_refuses_a_route_type_it_does_not_know():
    class UnknownRoute(BaseRoute):
        pass

    class Router:
        routes = [UnknownRoute()]

    with pytest.raises(TypeError, match="UnknownRoute"):
        list(iter_api_routes(Router()))


@pytest.mark.api
@pytest.mark.unit
@pytest.mark.parametrize("path", ["/documents/summary", "/documents/cohorts"])
def test_collection_routes_are_not_taken_for_a_workflow_id(test_client, path):
    response = test_client.get(path)

    assert response.status_code == 200
    assert "total_documents" in response.json()
