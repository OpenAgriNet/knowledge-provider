from urllib.parse import parse_qs

import pytest
from urllib3 import PoolManager

from pipeline import object_storage
from pipeline.object_storage.adapters import s3


def test_existing_minio_settings_remain_compatible():
    settings = {
        "MINIO_ENDPOINT": "minio:9000", "MINIO_BUCKET": "docs",
        "MINIO_ACCESS_KEY": "fixture-access", "MINIO_SECRET_KEY": "fixture-secret",
        "AWS_REGION": "us-west-2",
    }
    config = object_storage.load_object_store_config(settings)

    assert (config.provider, config.endpoint, config.bucket) == ("minio", "minio:9000", "docs")
    assert not config.tls
    assert config.create_bucket
    assert config.region is None
    assert "fixture-access" not in repr(config)
    assert "fixture-secret" not in repr(config)


def test_s3_static_defaults_to_tls_and_does_not_create_a_bucket():
    config = object_storage.load_object_store_config({
        "BLOB_STORAGE_PROVIDER": "s3", "BLOB_STORAGE_BUCKET": "existing-documents",
        "AWS_ACCESS_KEY_ID": "fixture-access", "AWS_SECRET_ACCESS_KEY": "fixture-secret",
        "AWS_SESSION_TOKEN": "fixture-session",
    })
    assert config.tls
    assert not config.create_bucket
    assert config.endpoint == "s3.amazonaws.com"
    assert config.session_token == "fixture-session"


def test_explicit_storage_credentials_cannot_mix_with_ambient_keys():
    with pytest.raises(RuntimeError, match="BLOB_STORAGE_SECRET_KEY"):
        object_storage.load_object_store_config({
            "BLOB_STORAGE_PROVIDER": "s3", "BLOB_STORAGE_BUCKET": "documents",
            "BLOB_STORAGE_ACCESS_KEY": "fixture-access", "AWS_SECRET_ACCESS_KEY": "other-secret",
        })


@pytest.mark.parametrize("endpoint", ["http://minio:9000", "user:password@minio", "minio/a", "minio:invalid"])
def test_storage_endpoint_errors_do_not_disclose_the_value(endpoint):
    with pytest.raises(RuntimeError, match="BLOB_STORAGE_ENDPOINT") as error:
        object_storage.load_object_store_config({"BLOB_STORAGE_ENDPOINT": endpoint})
    assert endpoint not in str(error.value)


def test_workload_identity_uses_the_configured_token_file_without_static_keys(monkeypatch):
    settings = {
        "BLOB_STORAGE_PROVIDER": "s3", "BLOB_STORAGE_BUCKET": "documents",
        "BLOB_STORAGE_CREDENTIALS": "aws_web_identity", "AWS_REGION": "us-west-2",
        "AWS_ROLE_ARN": "arn:aws:iam::000000000000:role/fixture",
        "AWS_WEB_IDENTITY_TOKEN_FILE": "/unused-fixture-token-path",
    }
    config = object_storage.load_object_store_config(settings)
    provider_settings = {}
    client_settings = {}

    def provider(**kwargs):
        provider_settings.update(kwargs)
        return "workload-provider"

    def client(endpoint, **kwargs):
        client_settings.update(kwargs)
        return "object-store"

    monkeypatch.setattr(s3, "_WebIdentityProvider", provider)
    monkeypatch.setattr(s3, "Minio", client)
    try:
        assert isinstance(object_storage.open_object_store(config), object_storage.S3ObjectStore)
    finally:
        object_storage.open_object_store.cache_clear()
    assert provider_settings["token_file"] == settings["AWS_WEB_IDENTITY_TOKEN_FILE"]
    assert provider_settings["custom_endpoint"] == "https://sts.us-west-2.amazonaws.com"
    assert client_settings["credentials"] == "workload-provider"
    assert client_settings["access_key"] is None
    assert client_settings["secret_key"] is None


def test_web_identity_transport_keeps_tokens_in_the_form_body(monkeypatch):
    def capture(self, method, url, **options):
        assert method == "POST"
        assert url == "https://sts.amazonaws.com"
        assert options["redirect"] is False
        assert parse_qs(options["body"].decode()) == {"WebIdentityToken": ["fixture-token"]}
        return "captured"

    monkeypatch.setattr(PoolManager, "urlopen", capture)
    transport = s3._TokenPostTransport()
    assert transport.urlopen("POST", "https://sts.amazonaws.com?WebIdentityToken=fixture-token") == "captured"


def test_web_identity_errors_hide_upstream_request_details(monkeypatch):
    def reject(self):
        raise ValueError("WebIdentityToken=fixture-token")

    monkeypatch.setattr(s3.IamAwsProvider, "retrieve", reject)
    with pytest.raises(RuntimeError, match="Unable to obtain object storage credentials") as error:
        s3._WebIdentityProvider().retrieve()
    assert "fixture-token" not in str(error.value)
    assert error.value.__suppress_context__


def test_pipeline_config_uses_storage_settings_and_hides_database_url(monkeypatch, capsys):
    from pipeline.config import load_config, print_config_status

    monkeypatch.setenv("BLOB_STORAGE_PROVIDER", "s3")
    monkeypatch.setenv("BLOB_STORAGE_BUCKET", "documents")
    monkeypatch.setenv("BLOB_STORAGE_ACCESS_KEY", "fixture-access")
    monkeypatch.setenv("BLOB_STORAGE_SECRET_KEY", "fixture-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/fixture")
    config = load_config()
    assert config.object_store.endpoint == "s3.amazonaws.com"
    assert config.object_store.bucket == "documents"
    print_config_status()
    output = capsys.readouterr().out
    assert "configuration is valid" in output
    assert "postgresql://localhost/fixture" not in output


def test_scoped_s3_configuration_takes_precedence_and_supports_secret_files(tmp_path):
    secret = tmp_path / "storage-secret"
    secret.write_text("fixture-secret\n")

    config = object_storage.load_object_store_config({
        "OBJECT_STORE_TYPE": "s3",
        "OBJECT_STORE_ENDPOINT": "http://object-store:9000",
        "OBJECT_STORE_BUCKET": "documents",
        "OBJECT_STORE_ACCESS_KEY_ID": "fixture-access",
        "OBJECT_STORE_SECRET_ACCESS_KEY_FILE": str(secret),
        "OBJECT_STORE_CREATE_BUCKET": "true",
        "MINIO_ENDPOINT": "obsolete",
    })

    assert config.endpoint == "object-store:9000"
    assert config.create_bucket
    assert not config.tls
    assert config.secret_key == "fixture-secret"
    assert "fixture-secret" not in repr(config)


def test_storage_adapter_reads_legacy_and_new_references_and_releases_stream():
    from unittest.mock import MagicMock

    client = MagicMock()
    response = client.get_object.return_value
    response.stream.return_value = iter((b"first", b"second"))
    store = object_storage.S3ObjectStore(client)
    ref = object_storage.parse_object_uri("minio:/documents/path/file.pdf")
    assert ref == object_storage.parse_object_uri(ref.to_uri())

    stream = store.open(ref)
    assert next(stream) == b"first"
    stream.close()

    client.get_object.assert_called_once_with("documents", "path/file.pdf")
    response.close.assert_called_once()
    response.release_conn.assert_called_once()


def test_static_s3_accepts_standard_aws_credentials():
    cfg = object_storage.load_object_store_config({
        "OBJECT_STORE_TYPE": "s3",
        "OBJECT_STORE_BUCKET": "docs",
        "AWS_ACCESS_KEY_ID": "fixture-aws-id",
        "AWS_SECRET_ACCESS_KEY": "fixture-aws-secret",
        "AWS_SESSION_TOKEN": "fixture-aws-token",
    })
    assert (cfg.access_key, cfg.secret_key, cfg.session_token) == (
        "fixture-aws-id", "fixture-aws-secret", "fixture-aws-token",
    )


def test_static_s3_does_not_mix_scoped_and_aws_credentials():
    settings = {
        "OBJECT_STORE_TYPE": "s3",
        "OBJECT_STORE_BUCKET": "docs",
        "AWS_ACCESS_KEY_ID": "fixture-aws-id",
        "AWS_SECRET_ACCESS_KEY": "fixture-aws-secret",
        "AWS_SESSION_TOKEN": "fixture-aws-token",
        "OBJECT_STORE_ACCESS_KEY_ID": "fixture-scoped-id",
    }
    with pytest.raises(RuntimeError, match="complete"):
        object_storage.load_object_store_config(settings)
    cfg = object_storage.load_object_store_config({
        **settings, "OBJECT_STORE_SECRET_ACCESS_KEY": "fixture-scoped-secret",
    })
    assert (cfg.access_key, cfg.secret_key, cfg.session_token) == (
        "fixture-scoped-id", "fixture-scoped-secret", None,
    )
