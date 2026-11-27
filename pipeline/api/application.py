"""FastAPI construction, middleware, and exception handling."""

import asyncio
import re
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from . import api as _api
from . import request_logging
from .rate_limiting import limiter

_DOCUMENT_PATH_RE = re.compile(r"^/documents/([^/]+)(/.*)?$")
# Only identifier shapes with an indexed lookup are resolved before auth runs:
# the document UUID, the 32-hex fingerprint, or a ``doc-<md5>`` workflow id.
_RESOLVABLE_IDENTIFIER_RE = re.compile(
    r"^(?:[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}|[0-9a-f]{32}|doc-[0-9a-f]{32}.*)$"
)
_static_document_segments: Optional[set[str]] = None


def _static_document_path_segments() -> set[str]:
    """Literal second segments of ``/documents/...`` routes (``upload``, ``batch``...)."""
    global _static_document_segments
    if _static_document_segments is None:
        segments = set()
        for route in _api.app.routes:
            match = _DOCUMENT_PATH_RE.match(getattr(route, "path", ""))
            if match and "{" not in match.group(1):
                segments.add(match.group(1))
        _static_document_segments = segments
    return _static_document_segments


async def canonicalize_document_identifier(request: Request, call_next):
    """Let ``/documents/{id}/...`` take the document UUID or any legacy identifier.

    Handlers address a document by its Temporal workflow id (they signal and
    query Temporal with it), so the path is rewritten to that id before routing.
    An identifier of another shape, or one that matches no document, is left alone and
    404s in the handler; the lookup runs before auth, so it is limited to cheap shapes.
    """
    match = _DOCUMENT_PATH_RE.match(request.scope["path"])
    if (
        match
        and _RESOLVABLE_IDENTIFIER_RE.match(match.group(1))
        and match.group(1) not in _api._static_document_path_segments()
    ):
        document = await asyncio.to_thread(_api.db.find_document_by_doc_identifier, match.group(1))
        if document and document["workflow_id"] and document["workflow_id"] != match.group(1):
            new_path = f"/documents/{document['workflow_id']}{match.group(2) or ''}"
            request.scope["path"] = new_path
            request.scope["raw_path"] = quote(new_path, safe="/:").encode("ascii")
    return await call_next(request)


API_DESCRIPTION = """
REST API for the Temporal-based document OCR pipeline with translation support.

## Workflow Stages

1. `registered` - Document registered
2. `ocr_processing` - OCR in progress
3. `ocr_review` - **Waiting for OCR review/approval**
4. `translation_processing` - Translating non-English content
5. `translation_review` - **Waiting for translation review/approval**
6. `chunking` - Chunking in progress
7. `chunk_review` - **Waiting for chunk review/approval**
8. `ready_for_ingestion` - **Waiting for final approval before DEV ingest**
9. `ingesting` - Ingesting to DEV Qdrant
10. `approval_for_prod` - **Waiting for superadmin prod approval**
11. `ingesting_prod` - Promoting vectors to PROD Qdrant
12. `completed` - Done
13. `failed` - Error occurred

## Review Flow

1. Start workflow with `POST /upload` or `POST /documents`
2. Wait for `ocr_review` stage
3. Review/edit pages with `GET/PATCH /documents/{id}/pages/{num}`
4. Approve with `POST /documents/{id}/approve-ocr`
5. Wait for `translation_review` stage
6. Review/edit translations with `PATCH /documents/{id}/pages/{num}`
7. Approve with `POST /documents/{id}/approve-translation`
8. Wait for `chunk_review` stage
9. Review/edit chunks with `GET/PATCH /documents/{id}/chunks/{num}`
10. Approve with `POST /documents/{id}/approve-chunks`
11. Wait for `ready_for_ingestion` stage
12. Approve DEV ingest with `POST /documents/{id}/approve-ingestion`
13. Wait for `approval_for_prod` stage
14. Superadmin promote with `POST /documents/{id}/approve-prod` → `ingesting_prod` → `completed`
    """


def create_app(lifespan):
    app = FastAPI(
        title="Document Ingestion Pipeline API",
        description=API_DESCRIPTION,
        version="2.0.0",
        lifespan=lifespan
    )

    # CORS configuration - explicit origins for security
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_api.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    request_logging.install(app)

    app.middleware("http")(canonicalize_document_identifier)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, request_logging.recording(_rate_limit_exceeded_handler))
    return app
