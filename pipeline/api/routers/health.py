"""Runtime health endpoint."""

import asyncio

from fastapi import APIRouter

from pipeline.api import api as _api

router = APIRouter()


@router.get("/health")
async def health():
    """Health check."""
    return {
        "status": "ok",
        "temporal_connected": _api.temporal_client is not None,
        "database": await asyncio.to_thread(_api.db.check_health),
    }
