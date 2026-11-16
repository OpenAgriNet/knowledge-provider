"""Worker helpers for local copies and processing artifacts."""

import json
import mimetypes
import os
import re
import tempfile
from pathlib import Path

from pipeline.object_storage import (
    ObjectReference,
    ObjectStore,
    load_object_store_config,
    open_object_store,
    parse_object_uri,
)


def get_object_store() -> ObjectStore:
    return open_object_store(load_object_store_config())


def download_object(storage_uri: str) -> str:
    ref = parse_object_uri(storage_uri)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(ref.key).suffix) as file:
            temp_path = file.name
            for chunk in get_object_store().open(ref):
                file.write(chunk)
        return temp_path
    except Exception:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)
        raise


def _upload_artifact(local_path: str, workflow_id: str, artifact_type: str, filename: str) -> tuple[str, int, str]:
    config = load_object_store_config()
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "artifact")
    ref = ObjectReference(config.bucket, f"documents/{workflow_id}/{artifact_type}/{safe_name}")
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    size_bytes = os.path.getsize(local_path)
    with open(local_path, "rb") as file:
        open_object_store(config).put(ref, file, size_bytes, mime_type)
    return ref.to_uri(), size_bytes, mime_type


def _write_json_temp(data: object, suffix: str = ".json") -> str:
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=suffix, encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        return f.name
