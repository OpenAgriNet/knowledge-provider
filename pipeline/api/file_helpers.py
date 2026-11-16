"""File admission and source identity helpers for API intake."""

import hashlib
import time
from pathlib import Path

from fastapi import HTTPException

from pipeline.documents.document_formats import allowed_file_extensions
from ..object_storage import is_object_uri, parse_object_uri

def validate_file_path(filepath: str) -> str:
    """Accept a supported object storage reference, never a host path."""
    if not is_object_uri(filepath):
        raise HTTPException(403, "Host file registration is no longer supported. Upload the file instead.")
    try:
        parse_object_uri(filepath)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    suffix = Path(filepath).suffix.lower()
    if suffix not in allowed_file_extensions():
        raise HTTPException(400, f"Unsupported file type: {suffix}")
    return filepath


def get_filename_from_path(filepath: str) -> str:
    """Extract filename from a filepath string (works for local paths and object references)."""
    if is_object_uri(filepath):
        return filepath.split("/")[-1]
    return Path(filepath).name


def get_workflow_id(filepath: str) -> str:
    """Generate consistent workflow ID from filepath."""
    return f"doc-{hashlib.md5(filepath.encode()).hexdigest()[:12]}"


def _rerun_workflow_id(base_workflow_id: str) -> str:
    """Generate a fresh workflow ID for explicit reruns of the same source."""
    return f"{base_workflow_id}-rerun-{int(time.time())}"
