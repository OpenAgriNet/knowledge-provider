"""Object storage settings and legacy configuration compatibility."""

import os
import re
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ObjectStoreConfig:
    provider: str
    endpoint: str
    bucket: str
    tls: bool
    create_bucket: bool
    credentials_mode: str
    region: str | None = None
    project: str | None = None
    access_key: str | None = field(default=None, repr=False)
    secret_key: str | None = field(default=None, repr=False)
    session_token: str | None = field(default=None, repr=False)
    role_arn: str | None = field(default=None, repr=False)
    token_file: str | None = field(default=None, repr=False)


def _boolean(settings: Mapping[str, str], name: str, default: bool) -> bool:
    if name not in settings:
        return default
    value = settings[name].strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")


def load_object_store_config(settings: Mapping[str, str] | None = None) -> ObjectStoreConfig:
    settings = os.environ if settings is None else settings
    if not any(name.startswith("OBJECT_STORE_") for name in settings):
        return _load_legacy_config(settings)
    provider = settings.get("OBJECT_STORE_TYPE", "").strip().lower()
    if provider == "gcs":
        return _load_gcs_config(settings)
    if provider != "s3":
        raise RuntimeError("OBJECT_STORE_TYPE must be s3 or gcs")
    mode = settings.get("OBJECT_STORE_CREDENTIALS_TYPE", "static").lower()
    if mode not in {"static", "aws_web_identity"}:
        raise RuntimeError("OBJECT_STORE_CREDENTIALS_TYPE must be static or aws_web_identity")

    def credential(name: str, default_env: str | None = None) -> str | None:
        file = settings.get(f"{name}_FILE")
        value = settings.get(name)
        if file and value:
            raise RuntimeError(f"Configure only one of {name} and {name}_FILE")
        if file:
            try:
                with open(file, encoding="utf-8") as secret:
                    value = secret.read(16385).strip()
                if not value or len(value) > 16384:
                    raise ValueError
            except (OSError, ValueError):
                raise RuntimeError(f"Unable to read {name}_FILE") from None
        if value is None and default_env is not None:
            value = settings.get(default_env)
        return value

    endpoint = settings.get("OBJECT_STORE_ENDPOINT", "https://s3.amazonaws.com")
    try:
        if not isinstance(endpoint, str):
            raise ValueError
        parsed = urlsplit(endpoint)
        parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment
                or any(ch.isspace() or ord(ch) < 32 for ch in endpoint)):
            raise ValueError
    except ValueError:
        raise RuntimeError("OBJECT_STORE_ENDPOINT must be an HTTP(S) origin") from None

    bucket = settings.get("OBJECT_STORE_BUCKET")
    region = settings.get("OBJECT_STORE_REGION")
    create_bucket = _boolean(settings, "OBJECT_STORE_CREATE_BUCKET", False)
    if not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise RuntimeError("OBJECT_STORE_BUCKET must be an S3 bucket name")
    if region is not None and (not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[1-9][0-9]*", region)):
        raise RuntimeError("OBJECT_STORE_REGION must be an AWS region")

    options: dict[str, str | None]
    if mode == "static":
        # Keep credential families together: never pair a scoped key with an ambient secret.
        scoped_names = ("OBJECT_STORE_ACCESS_KEY_ID", "OBJECT_STORE_SECRET_ACCESS_KEY", "OBJECT_STORE_SESSION_TOKEN")
        explicit = any(settings.get(key) or settings.get(f"{key}_FILE") for key in scoped_names)
        if explicit:
            access_key, secret_key, token = (credential(key) for key in scoped_names)
        else:
            access_key = settings.get("AWS_ACCESS_KEY_ID")
            secret_key = settings.get("AWS_SECRET_ACCESS_KEY")
            token = settings.get("AWS_SESSION_TOKEN")
        if not access_key or not secret_key:
            raise RuntimeError("Set a complete AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY pair or an explicit OBJECT_STORE_* pair")
        options = {"access_key": access_key, "secret_key": secret_key, "session_token": token}
    else:
        role_arn = credential("OBJECT_STORE_ROLE_ARN", "AWS_ROLE_ARN")
        token_file = credential("OBJECT_STORE_TOKEN_FILE", "AWS_WEB_IDENTITY_TOKEN_FILE")
        if parsed.scheme != "https" or not role_arn or not token_file:
            raise RuntimeError("Object storage AWS web identity requires HTTPS, a role ARN, and a token file")
        options = {"role_arn": role_arn, "token_file": token_file}
    return ObjectStoreConfig(
        provider="s3", endpoint=parsed.netloc, bucket=bucket, tls=parsed.scheme == "https",
        create_bucket=create_bucket, credentials_mode=mode, region=region, **options,
    )


def _load_gcs_config(settings: Mapping[str, str]) -> ObjectStoreConfig:
    mode = settings.get("OBJECT_STORE_CREDENTIALS_TYPE", "google_adc").strip().lower()
    if mode != "google_adc":
        raise RuntimeError("Google Cloud Storage requires OBJECT_STORE_CREDENTIALS_TYPE=google_adc")
    if settings.get("OBJECT_STORE_ENDPOINT", "").strip():
        raise RuntimeError("Unset OBJECT_STORE_ENDPOINT when using Google Cloud Storage")

    bucket = settings.get("OBJECT_STORE_BUCKET", "").strip()
    max_length = 222 if "." in bucket else 63
    is_valid = (
        3 <= len(bucket) <= max_length
        and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?", part) for part in bucket.split("."))
        and not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", bucket)
        and not bucket.startswith("goog")
        and "google" not in bucket
    )
    if not is_valid:
        raise RuntimeError("OBJECT_STORE_BUCKET must be a Google Cloud Storage bucket name")

    location = settings.get("OBJECT_STORE_REGION", "").strip() or None
    if location and not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]{1,31}", location):
        raise RuntimeError("OBJECT_STORE_REGION must be a Google Cloud Storage location")
    return ObjectStoreConfig(
        provider="gcs", endpoint="storage.googleapis.com", bucket=bucket, tls=True,
        create_bucket=_boolean(settings, "OBJECT_STORE_CREATE_BUCKET", False), credentials_mode=mode,
        region=location, project=settings.get("GOOGLE_CLOUD_PROJECT", "").strip() or None,
    )


def _load_legacy_config(settings: Mapping[str, str]) -> ObjectStoreConfig:
    provider = settings.get("BLOB_STORAGE_PROVIDER", "minio").strip().lower()
    if provider not in {"minio", "s3"}:
        raise RuntimeError("BLOB_STORAGE_PROVIDER must be minio or s3")
    legacy = provider == "minio"
    default_endpoint = settings.get("MINIO_ENDPOINT", "localhost:9000") if legacy else "s3.amazonaws.com"
    endpoint = settings.get("BLOB_STORAGE_ENDPOINT", default_endpoint).strip()
    try:
        parsed = urlsplit(f"//{endpoint}")
        parsed.port  # Validate the optional port before constructing the SDK client.
        if (not parsed.hostname or parsed.username is not None or parsed.path or parsed.query or parsed.fragment
                or any(character.isspace() or ord(character) < 32 for character in endpoint)):
            raise ValueError
    except ValueError:
        raise RuntimeError("BLOB_STORAGE_ENDPOINT must be a host with an optional port") from None

    bucket = settings.get("BLOB_STORAGE_BUCKET", settings.get("MINIO_BUCKET", "documents") if legacy else "").strip()
    if not bucket:
        raise RuntimeError("BLOB_STORAGE_BUCKET is required")
    tls = _boolean(settings, "BLOB_STORAGE_TLS", default=not legacy)
    mode = settings.get("BLOB_STORAGE_CREDENTIALS", "static").strip().lower()
    region = settings.get("BLOB_STORAGE_REGION") or (None if legacy else settings.get("AWS_REGION"))
    options: dict[str, str | None] = {}
    if mode == "static":
        access_var, secret_var, token_var = (
            ("MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_SESSION_TOKEN") if legacy
            else ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
        )
        explicit = ("BLOB_STORAGE_ACCESS_KEY", "BLOB_STORAGE_SECRET_KEY", "BLOB_STORAGE_SESSION_TOKEN")
        if any(name in settings for name in explicit):
            access_var, secret_var, token_var = explicit
        options = {"access_key": settings.get(access_var), "secret_key": settings.get(secret_var),
                   "session_token": settings.get(token_var)}
        if not options["access_key"] or not options["secret_key"]:
            raise RuntimeError(f"Static object storage credentials are required ({access_var} and {secret_var})")
    elif mode == "aws_web_identity":
        if legacy or not tls:
            raise RuntimeError("AWS web identity requires the s3 provider with TLS")
        options = {"role_arn": settings.get("AWS_ROLE_ARN"), "token_file": settings.get("AWS_WEB_IDENTITY_TOKEN_FILE")}
        if not options["role_arn"] or not options["token_file"]:
            raise RuntimeError("AWS_ROLE_ARN and AWS_WEB_IDENTITY_TOKEN_FILE are required for AWS web identity")
        if region and not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[1-9][0-9]*", region):
            raise RuntimeError("AWS web identity requires a valid AWS region")
    else:
        raise RuntimeError("BLOB_STORAGE_CREDENTIALS must be static or aws_web_identity")

    return ObjectStoreConfig(
        provider=provider, endpoint=endpoint, bucket=bucket, tls=tls,
        create_bucket=_boolean(settings, "BLOB_STORAGE_CREATE_BUCKET", default=legacy),
        credentials_mode=mode, region=region,
        **options,
    )
