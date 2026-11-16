"""Object references and the storage operations required by ingestion."""

from dataclasses import dataclass
from typing import BinaryIO, Iterable, Protocol


@dataclass(frozen=True)
class ObjectReference:
    bucket: str
    key: str

    def to_uri(self) -> str:
        return f"blob://{self.bucket}/{self.key}"


def is_object_uri(value: str) -> bool:
    return value.startswith(("blob://", "minio:/"))


def parse_object_uri(value: str) -> ObjectReference:
    if not is_object_uri(value):
        raise ValueError("Expected an object storage reference")
    path = value.removeprefix("blob://") if value.startswith("blob://") else value.removeprefix("minio:/").removeprefix("/")
    bucket, _, key = path.partition("/")
    if not bucket or not key:
        raise ValueError("An object storage reference requires a bucket and key")
    return ObjectReference(bucket, key)


class ObjectStore(Protocol):
    """Object operations required by the API and worker."""

    def ensure_bucket(self, bucket: str) -> None: ...
    def open(self, ref: ObjectReference) -> Iterable[bytes]: ...
    def delete(self, ref: ObjectReference) -> None: ...
    def put(self, ref: ObjectReference, data: BinaryIO, size: int, content_type: str) -> None: ...
