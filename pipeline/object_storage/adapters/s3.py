"""S3-compatible storage and AWS web identity credentials."""

from typing import BinaryIO, Iterable
from urllib.parse import urlsplit

from minio import Minio
from minio.credentials import IamAwsProvider
from urllib3 import PoolManager, Timeout

from ..config import ObjectStoreConfig
from ..contract import ObjectReference, ObjectStore


def create_s3_object_store(config: ObjectStoreConfig) -> ObjectStore:
    credentials = None
    if config.credentials_mode == "aws_web_identity":
        endpoint = "https://sts.amazonaws.com"
        if config.region:
            suffix = "amazonaws.com.cn" if config.region.startswith("cn-") else "amazonaws.com"
            endpoint = f"https://sts.{config.region}.{suffix}"
        credentials = _WebIdentityProvider(
            token_file=config.token_file, role_arn=config.role_arn, custom_endpoint=endpoint,
            http_client=_TokenPostTransport(timeout=Timeout(connect=5, read=10), retries=False),
        )
    return S3ObjectStore(Minio(
        config.endpoint, secure=config.tls, region=config.region, credentials=credentials,
        access_key=config.access_key, secret_key=config.secret_key, session_token=config.session_token,
    ))


class S3ObjectStore:
    """Translate application storage operations into the S3 client's calls."""

    def __init__(self, client: Minio):
        self._client = client

    def ensure_bucket(self, bucket: str) -> None:
        if not self._client.bucket_exists(bucket):
            self._client.make_bucket(bucket)

    def open(self, ref: ObjectReference) -> Iterable[bytes]:
        def stream():
            response = self._client.get_object(ref.bucket, ref.key)
            try:
                yield from response.stream(64 * 1024)
            finally:
                response.close()
                response.release_conn()

        return stream()

    def delete(self, ref: ObjectReference) -> None:
        self._client.remove_object(ref.bucket, ref.key)

    def put(self, ref: ObjectReference, data: BinaryIO, size: int, content_type: str) -> None:
        self._client.put_object(ref.bucket, ref.key, data, size, content_type=content_type)


class _TokenPostTransport(PoolManager):
    def urlopen(self, method, url, **kwargs):
        # The SDK supplies STS POST fields as a query string. Send those fields
        # in the form body so HTTP request logs and retry errors omit the token.
        target = urlsplit(url)
        return super().urlopen(
            method, target._replace(query="").geturl(), body=target.query.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"}, redirect=False,
        )


class _WebIdentityProvider(IamAwsProvider):
    def retrieve(self):
        try:
            return super().retrieve()
        except Exception:
            # Some SDK errors still quote the original URL, including its token.
            raise RuntimeError("Unable to obtain object storage credentials using AWS web identity") from None
