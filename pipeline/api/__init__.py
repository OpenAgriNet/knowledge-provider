"""HTTP application entry point; feature handlers live in ``routers/``."""

from .api import app

__all__ = ["app"]
