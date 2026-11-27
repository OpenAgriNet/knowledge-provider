"""Google Cloud Storage backed by Application Default Credentials."""

from collections.abc import Iterable
from typing import BinaryIO

from google.api_core.exceptions import Conflict, NotFound
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import storage

from ..config import ObjectStoreConfig
from ..contract import ObjectReference

_DOWNLOAD_BUFFER = 1024 * 1024
_STREAM_BLOCK = 64 * 1024
_UPLOAD_BUFFER = 8 * 1024 * 1024


def create_gcs_object_store(config: ObjectStoreConfig) -> "GCSObjectStore":
    try:
        client = storage.Client(project=config.project)
    except DefaultCredentialsError:
        raise RuntimeError("Unable to load Google Cloud Application Default Credentials") from None
    return GCSObjectStore(client, location=config.region)


class GCSObjectStore:
    def __init__(self, client: storage.Client, *, location: str | None = None):
        self._client = client
        self._location = location

    def ensure_bucket(self, bucket: str) -> None:
        target = self._client.bucket(bucket)
        if target.exists():
            return
        try:
            self._client.create_bucket(target, location=self._location)
        except Conflict:
            if not target.exists():
                raise

    def open(self, ref: ObjectReference) -> Iterable[bytes]:
        blob = self._client.bucket(ref.bucket).blob(ref.key)
        # A range reader must fetch each part from the same object generation.
        blob.reload()
        with blob.open(
            "rb", chunk_size=_DOWNLOAD_BUFFER, raw_download=True, if_generation_match=blob.generation,
        ) as reader:
            yield from iter(lambda: reader.read(_STREAM_BLOCK), b"")

    def delete(self, ref: ObjectReference) -> None:
        blob = self._client.bucket(ref.bucket).blob(ref.key)
        try:
            blob.reload()
            blob.delete(if_generation_match=blob.generation)
        except NotFound:
            pass

    def put(self, ref: ObjectReference, data: BinaryIO, size: int, content_type: str) -> None:
        blob = self._client.bucket(ref.bucket).blob(ref.key, chunk_size=_UPLOAD_BUFFER)
        blob.upload_from_file(data, size=size, content_type=content_type, checksum="auto")
