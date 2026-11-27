"""Object storage used by the ingestion API and worker."""

from .adapters.s3 import S3ObjectStore
from .config import ObjectStoreConfig, load_object_store_config
from .contract import ObjectReference, ObjectStore, is_object_uri, parse_object_uri
from .service import open_object_store

__all__ = [
    "ObjectReference",
    "ObjectStore",
    "ObjectStoreConfig",
    "S3ObjectStore",
    "is_object_uri",
    "load_object_store_config",
    "open_object_store",
    "parse_object_uri",
]
