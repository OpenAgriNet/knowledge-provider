"""Construct the configured object storage adapter."""

from functools import lru_cache

from .adapters.s3 import create_s3_object_store
from .config import ObjectStoreConfig
from .contract import ObjectStore


@lru_cache(maxsize=1)
def open_object_store(config: ObjectStoreConfig) -> ObjectStore:
    if config.provider in {"minio", "s3"}:
        return create_s3_object_store(config)
    if config.provider == "gcs":
        from .adapters.gcs import create_gcs_object_store

        return create_gcs_object_store(config)
    raise ValueError("Unsupported object storage provider")
