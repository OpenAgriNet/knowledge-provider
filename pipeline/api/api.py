"""
FastAPI REST API for the Temporal-based OCR pipeline.

Composition and dependency overrides live here; feature routes and shared helpers
are grouped in the adjacent modules.
"""


# Imports are also the supported runtime dependency override boundary.
# ruff: noqa: F401
import logging
import os
from pathlib import Path
from typing import Optional

# Load repo-root .env so KEYCLOAK_ADMIN_* etc. work without manual `source .env`
try:
    from dotenv import load_dotenv

    # parents[0] = pipeline/api, [1] = pipeline, [2] = repo root
    _env_path = Path(__file__).resolve().parents[2] / ".env"
    if _env_path.is_file():
        load_dotenv(_env_path, override=False)
except Exception:
    pass

from fastapi import FastAPI, HTTPException, Request
from temporalio.client import Client

from pipeline import db
from pipeline.documents.document_formats import allowed_file_extensions, installed_formats
from pipeline.documents.document_kinds import validate_document_kind
from pipeline.documents.models import DocumentStage, DocumentSummary
from pipeline.network.catalog import scheme_catalog
from pipeline.network.network_publisher import validate_network_adapters
from pipeline.network.release_policy import prod_stage_disabled
from pipeline.telemetry.logs import configure_console, configure_logs, reset_logs

from ..auth.config import load_auth_config, validate_auth_config
from ..auth.models import AuthUser
from ..auth.permissions import Permission
from ..auth.tenancy import (
    allowed_instances,
    assert_document_instance_access,
    normalize_instance,
    resolve_create_instance,
)
from ..object_storage import (
    ObjectStore,
    is_object_uri,
    load_object_store_config,
    open_object_store,
    parse_object_uri,
)
from .rate_limiting import limiter

LOG_LEVEL = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").strip().upper(), logging.INFO)
# uvicorn only configures its own "uvicorn"/"uvicorn.access" loggers, so plain
# logging.info() calls in this module (e.g. /search) are silently dropped
# without configuring the root logger too - same LOG_LEVEL convention as
# pipeline/worker.py.
configure_console(LOG_LEVEL)

logger = logging.getLogger(__name__)

TASK_QUEUE = os.environ.get("TEMPORAL_TASK_QUEUE", "ocr-pipeline")

# Global clients
temporal_client: Optional[Client] = None
object_store: Optional[ObjectStore] = None
OBJECT_STORE_BUCKET = "documents"


from .application import (  # noqa: E402
    _static_document_path_segments,
    canonicalize_document_identifier,
    create_app,
)
from .audit_helpers import (  # noqa: E402
    _actor_from_user,
    _log_audit,
    _parse_roles_field,
)
from .document_helpers import (  # noqa: E402
    _assert_can_replace,
    _assert_upload_start_resolved,
    _build_provenance_links,
    _document_cohorts_payload,
    _document_for_user_or_none,
    _document_summary_from_row,
    _document_title,
    _instance_scope_for_user,
    _list_available_actions,
    _mark_reindex_required,
    _provenance_base_urls,
    _require_document_for_user,
    _resolve_create_instance,
    _resolve_document_identity,
)
from .file_helpers import (  # noqa: E402
    _rerun_workflow_id,
    get_filename_from_path,
    get_workflow_id,
    validate_file_path,
)
from .index_helpers import (  # noqa: E402
    delete_chunks_from_index,
    delete_single_chunk_from_index,
)
from .runtime import _start_services, lifespan  # noqa: E402

ALLOWED_ORIGINS = os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")
RATE_LIMIT_UPLOAD = os.environ.get("RATE_LIMIT_UPLOAD", "10/minute")

app = create_app(lifespan)

# Register routes after establishing the runtime dependency boundary.
from .routers.audit import router as audit_router  # noqa: E402
from .routers.auth_sessions import router as auth_sessions_router  # noqa: E402
from .routers.catalog import router as catalog_router  # noqa: E402
from .routers.chunks import router as chunks_router  # noqa: E402
from .routers.crawl import router as crawl_router  # noqa: E402
from .routers.document_content import router as document_content_router  # noqa: E402
from .routers.document_detail import router as document_detail_router  # noqa: E402
from .routers.document_lifecycle import router as document_lifecycle_router  # noqa: E402
from .routers.document_review import router as document_review_router  # noqa: E402
from .routers.documents import router as documents_router  # noqa: E402
from .routers.health import health  # noqa: E402
from .routers.health import router as health_router  # noqa: E402
from .routers.indexes import router as indexes_router  # noqa: E402
from .routers.operations import router as operations_router  # noqa: E402
from .routers.pages import router as pages_router  # noqa: E402
from .routers.providers import router as providers_router  # noqa: E402
from .routers.registry_proposals import router as registry_proposals_router  # noqa: E402
from .routers.search import router as search_router  # noqa: E402
from .routers.search_settings import router as search_settings_router  # noqa: E402
from .routers.upload import router as upload_router  # noqa: E402
from .routers.user_admin import router as user_admin_router  # noqa: E402
from .routers.webhook_sources import knowledge_router  # noqa: E402
from .routers.webhook_sources import router as webhook_sources_router  # noqa: E402

app.include_router(health_router)
app.include_router(search_router)
app.include_router(documents_router)
app.include_router(crawl_router)
app.include_router(providers_router)
app.include_router(upload_router)
app.include_router(webhook_sources_router)
app.include_router(knowledge_router)
app.include_router(registry_proposals_router)
app.include_router(auth_sessions_router)
app.include_router(user_admin_router)
app.include_router(catalog_router)
app.include_router(pages_router)
app.include_router(chunks_router)
app.include_router(search_settings_router)
app.include_router(operations_router)
app.include_router(document_detail_router)
app.include_router(document_lifecycle_router)
app.include_router(document_review_router)
app.include_router(audit_router)
app.include_router(document_content_router)
app.include_router(indexes_router)
