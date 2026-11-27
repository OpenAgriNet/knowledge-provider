import json
from io import BytesIO
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from google.api_core.exceptions import Conflict, NotFound, PreconditionFailed
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage
from requests import Request, Response, Session
from urllib3.response import HTTPResponse

from pipeline.object_storage import ObjectReference, load_object_store_config, open_object_store
from pipeline.object_storage.adapters.gcs import GCSObjectStore

SETTINGS = {
    "OBJECT_STORE_TYPE": "gcs",
    "OBJECT_STORE_BUCKET": "kp-documents",
    "GOOGLE_CLOUD_PROJECT": "kp-fixture-project",
}


def test_gcs_configuration_selects_google_adc(monkeypatch):
    client_factory = MagicMock()
    monkeypatch.setattr("pipeline.object_storage.adapters.gcs.storage.Client", client_factory)
    config = load_object_store_config(SETTINGS)

    assert config.credentials_mode == "google_adc"
    assert config.tls and not config.create_bucket
    try:
        assert isinstance(open_object_store(config), GCSObjectStore)
    finally:
        open_object_store.cache_clear()
    client_factory.assert_called_once_with(project="kp-fixture-project")


@pytest.mark.parametrize("override,expected", [
    ({"OBJECT_STORE_CREDENTIALS_TYPE": "static"}, "google_adc"),
    ({"OBJECT_STORE_ENDPOINT": "http://minio:9000"}, "Unset OBJECT_STORE_ENDPOINT"),
    ({"OBJECT_STORE_BUCKET": "invalid bucket"}, "OBJECT_STORE_BUCKET"),
])
def test_gcs_rejects_incompatible_settings(override, expected):
    with pytest.raises(RuntimeError, match=expected):
        load_object_store_config({**SETTINGS, **override})


def test_gcs_stream_is_lazy_pins_generation_and_closes_early():
    client = MagicMock()
    blob = client.bucket.return_value.blob.return_value
    reader = blob.open.return_value.__enter__.return_value
    reader.read.return_value = b"first block"

    stream = iter(GCSObjectStore(client).open(ObjectReference("kp-documents", "originals/a.pdf")))
    client.bucket.assert_not_called()
    assert next(stream) == b"first block"
    stream.close()

    client.bucket.assert_called_once_with("kp-documents")
    client.bucket.return_value.blob.assert_called_once_with("originals/a.pdf")
    assert blob.method_calls[0][0] == "reload"
    blob.open.assert_called_once_with(
        "rb", chunk_size=1024 * 1024, raw_download=True, if_generation_match=blob.generation,
    )
    blob.open.return_value.__exit__.assert_called_once()


def test_gcs_put_preserves_name_content_type_and_size():
    client = MagicMock()
    data = BytesIO(b"pdf")
    GCSObjectStore(client).put(ObjectReference("kp-documents", "in/a b.pdf"), data, 3, "application/pdf")
    client.bucket.assert_called_once_with("kp-documents")
    bucket = client.bucket.return_value
    bucket.blob.assert_called_once_with("in/a b.pdf", chunk_size=8 * 1024 * 1024)
    bucket.blob.return_value.upload_from_file.assert_called_once_with(
        data, size=3, content_type="application/pdf", checksum="auto",
    )


def test_gcs_delete_is_idempotent_without_deleting_a_new_object_generation():
    client = MagicMock()
    blob = client.bucket.return_value.blob.return_value
    blob.generation = 42
    ref = ObjectReference("kp-documents", "originals/a.pdf")
    store = GCSObjectStore(client)

    store.delete(ref)
    blob.delete.assert_called_once_with(if_generation_match=42)

    blob.delete.reset_mock()
    blob.reload.side_effect = NotFound("absent")
    store.delete(ref)
    blob.delete.assert_not_called()

    blob.reload.side_effect = None
    blob.delete.side_effect = PreconditionFailed("another generation")
    with pytest.raises(PreconditionFailed):
        store.delete(ref)


def test_gcs_bucket_creation_handles_a_concurrent_creator():
    client = MagicMock()
    bucket = client.bucket.return_value
    bucket.exists.side_effect = [False, True]
    client.create_bucket.side_effect = Conflict("already created")

    GCSObjectStore(client, location="asia-south1").ensure_bucket("kp-documents")

    client.create_bucket.assert_called_once_with(bucket, location="asia-south1")
    assert bucket.exists.call_count == 2


def test_gcs_sdk_uses_a_generation_precondition_on_object_requests():
    object_path = "/storage/v1/b/kp-documents/o/old%20reports%2Fa.pdf"
    media_path = "/download" + object_path
    metadata = {
        "name": "old reports/a.pdf",
        "bucket": "kp-documents",
        "generation": "42",
        "size": "3",
        # This metadata URL does not itself specify a generation.
        "mediaLink": f"https://storage.googleapis.com{media_path}?alt=media",
    }
    calls = []

    class StorageTransport(Session):
        is_mtls = False

        def request(self, method, url, **options):
            prepared = Request(method, url, params=options.get("params")).prepare()
            parsed = urlsplit(prepared.url)
            if parsed.path == "/storage/v1/b/kp-documents":
                return make_response(prepared, 200, b'{"name":"kp-documents"}')
            calls.append((method, parsed.path))
            if method == "POST":
                assert b"application/pdf" in options["data"]
                return make_response(prepared, 200, json.dumps(metadata).encode())
            if method == "GET" and parsed.path == object_path:
                return make_response(prepared, 200, json.dumps(metadata).encode())
            assert parse_qs(parsed.query)["ifGenerationMatch"] == ["42"]
            if method == "DELETE":
                return make_response(prepared, 204, b"")
            byte_range = options["headers"]["range"]
            if byte_range.startswith("bytes=3-"):
                return make_response(prepared, 416, b"")
            assert byte_range.startswith("bytes=0-")
            response = make_response(prepared, 206, b"pdf")
            response.headers["Content-Range"] = "bytes 0-2/3"
            return response

    def make_response(prepared, status, data):
        response = Response()
        response.status_code = status
        response.url = prepared.url
        response.request = prepared
        response.raw = HTTPResponse(body=BytesIO(data), preload_content=False)
        return response

    client = storage.Client(
        project="kp-fixture-project", credentials=AnonymousCredentials(), _http=StorageTransport(),
    )
    store = GCSObjectStore(client)
    ref = ObjectReference("kp-documents", "old reports/a.pdf")
    store.put(ref, BytesIO(b"pdf"), 3, "application/pdf")
    assert b"".join(store.open(ref)) == b"pdf"
    store.delete(ref)
    assert ("POST", "/upload/storage/v1/b/kp-documents/o") in calls
    assert ("GET", media_path) in calls
    assert ("DELETE", object_path) == calls[-1]
