"""API service startup and shutdown, using the composition module's dependencies."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from pipeline.documents.document_kinds import configured_document_kinds
from pipeline.metadata.intake_profiles import load_intake_profiles
from pipeline.network.release_policy import validate_projection_destinations

from . import api as _api


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize Temporal and object storage clients on startup."""
    _api.configure_logs()
    try:
        await _api._start_services()
    except Exception:
        logging.exception("api startup failed")
        _api.reset_logs()
        raise
    yield
    logging.info("api stopping")
    _api.reset_logs()


async def _start_services():
    _api.validate_network_adapters()
    _api.installed_formats()
    load_intake_profiles()
    configured_document_kinds()
    validate_projection_destinations()

    auth_cfg = _api.load_auth_config()
    _api.validate_auth_config(auth_cfg)
    if auth_cfg.disabled:
        logging.warning(
            "WARNING: AUTH_DISABLED=true — every caller is treated as synthetic "
            "master_admin with unrestricted instance access. Do not expose this "
            "API beyond the internal network or set AUTH_DISABLED=false until "
            "the maintainer UI sends Bearer tokens."
        )
    else:
        logging.info(
            "auth enabled",
            extra={
                "issuer": auth_cfg.keycloak_issuer,
                "audience": auth_cfg.keycloak_audience or "(none)",
                "jwks": auth_cfg.keycloak_jwks_url,
            },
        )

    logging.info("api startup: initializing PostgreSQL database")
    _api.db.init_db()

    # Temporal
    temporal_host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    logging.info("api startup: connecting to Temporal at %s", temporal_host)
    _api.temporal_client = await _api.Client.connect(temporal_host)

    storage = _api.load_object_store_config()
    _api.OBJECT_STORE_BUCKET = storage.bucket
    _api.object_store = _api.open_object_store(storage)
    if storage.create_bucket:
        await asyncio.to_thread(_api.object_store.ensure_bucket, _api.OBJECT_STORE_BUCKET)
        logging.info(
            "api startup: ensured object storage bucket", extra={"bucket": _api.OBJECT_STORE_BUCKET}
        )

    logging.info(
        "api started",
        extra={
            "auth": "disabled" if auth_cfg.disabled else "enabled",
            "temporal": temporal_host,
            "object_storage": storage.provider,
            "bucket": _api.OBJECT_STORE_BUCKET,
        },
    )
