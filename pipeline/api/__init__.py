"""HTTP API package. The FastAPI app lives in ``api.py``; per-resource routers sit beside it."""

from .api import app

__all__ = ["app"]
