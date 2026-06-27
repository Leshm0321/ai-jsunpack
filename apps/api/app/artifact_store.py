from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urlparse


class ArtifactStoreError(RuntimeError):
    pass


class ArtifactStoreConfigurationError(ArtifactStoreError):
    pass


@dataclass(frozen=True)
class StoredArtifactRef:
    storage_uri: str
    hash: str
    size: int


class ObjectStorageClient(Protocol):
    def put_object(
        self,
        bucket: str,
        key: str,
        content: bytes,
        *,
        metadata: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
    ) -> None:
        ...

    def get_object(self, bucket: str, key: str) -> bytes:
        ...

    def object_exists(self, bucket: str, key: str) -> bool:
        ...

    def list_objects(self, bucket: str, prefix: str) -> list[str]:
        ...

    def delete_object(self, bucket: str, key: str) -> None:
        ...


class Boto3ObjectStorageClient:
    """基于 boto3 的生产 S3/MinIO client。

    构造函数接受已创建的 client，便于测试或由模块外管理 boto3 session 的部署复用。
    """

    def __init__(
        self,
        *,
        s3_client: Any | None = None,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        addressing_style: str | None = None,
    ) -> None:
        self.client = s3_client or self._create_client(
            endpoint_url=endpoint_url,
            region_name=region_name,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            session_token=session_token,
            addressing_style=addressing_style,
        )

    def put_object(
        self,
        bucket: str,
        key: str,
        content: bytes,
        *,
        metadata: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": content,
        }
        if metadata:
            kwargs["Metadata"] = _normalize_object_metadata(metadata)
        if tags:
            kwargs["Tagging"] = urlencode(_normalize_object_tags(tags))
        self.client.put_object(**kwargs)

    def get_object(self, bucket: str, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
        except Exception as error:
            if _is_missing_object_error(error):
                raise FileNotFoundError(f"Object not found: s3://{bucket}/{key}") from error
            raise
        body = response["Body"]
        try:
            return body.read()
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    def object_exists(self, bucket: str, key: str) -> bool:
        try:
            self.client.head_object(Bucket=bucket, Key=key)
            return True
        except Exception as error:
            if _is_missing_object_error(error):
                return False
            raise

    def list_objects(self, bucket: str, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator_factory = getattr(self.client, "get_paginator", None)
        if callable(paginator_factory):
            paginator = paginator_factory("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                keys.extend(_object_keys_from_page(page))
            return sorted(keys)

        continuation_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            page = self.client.list_objects_v2(**kwargs)
            keys.extend(_object_keys_from_page(page))
            if not page.get("IsTruncated"):
                return sorted(keys)
            continuation_token = page.get("NextContinuationToken")
            if not continuation_token:
                return sorted(keys)

    def delete_object(self, bucket: str, key: str) -> None:
        self.client.delete_object(Bucket=bucket, Key=key)

    def presigned_get_object_url(self, bucket: str, key: str, *, expires_in_seconds: int) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in_seconds,
        )

    def configure_lifecycle_rules(
        self,
        bucket: str,
        rules: list[dict[str, Any]],
        *,
        managed_rule_id_prefix: str = "ai-jsunpack-",
    ) -> None:
        current_rules: list[dict[str, Any]] = []
        try:
            current_rules = self.client.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
        except Exception as error:
            if not _is_missing_lifecycle_error(error):
                raise

        retained_rules = [
            rule for rule in current_rules if not str(rule.get("ID", "")).startswith(managed_rule_id_prefix)
        ]
        self.client.put_bucket_lifecycle_configuration(
            Bucket=bucket,
            LifecycleConfiguration={"Rules": [*retained_rules, *rules]},
        )

    def _create_client(
        self,
        *,
        endpoint_url: str | None,
        region_name: str | None,
        access_key_id: str | None,
        secret_access_key: str | None,
        session_token: str | None,
        addressing_style: str | None,
    ) -> Any:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as error:
            raise ArtifactStoreConfigurationError(
                "S3/MinIO artifact store requires boto3. Install project Python dependencies first."
            ) from error

        client_kwargs: dict[str, Any] = {}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        if region_name:
            client_kwargs["region_name"] = region_name
        if access_key_id:
            client_kwargs["aws_access_key_id"] = access_key_id
        if secret_access_key:
            client_kwargs["aws_secret_access_key"] = secret_access_key
        if session_token:
            client_kwargs["aws_session_token"] = session_token
        if addressing_style and addressing_style != "auto":
            client_kwargs["config"] = Config(s3={"addressing_style": addressing_style})
        return boto3.client("s3", **client_kwargs)


class ArtifactStore(Protocol):
    def write_bytes(
        self,
        *,
        job_id: str,
        artifact_id: str,
        filename: str,
        content: bytes,
        metadata: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
    ) -> StoredArtifactRef:
        ...

    def copy_path(
        self,
        *,
        job_id: str,
        artifact_id: str,
        filename: str,
        source_path: Path | str,
        metadata: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
    ) -> StoredArtifactRef:
        ...

    def read_bytes(self, storage_uri: str) -> bytes:
        ...

    def exists(self, storage_uri: str) -> bool:
        ...

    def is_file(self, storage_uri: str) -> bool:
        ...

    def is_directory(self, storage_uri: str) -> bool:
        ...

    def local_path(self, storage_uri: str) -> Path | None:
        ...

    def materialize_directory(self, storage_uri: str, target_dir: Path | str) -> Path:
        ...

    def filename(self, storage_uri: str) -> str:
        ...

    def suffix(self, storage_uri: str) -> str:
        ...

    def delete(self, storage_uri: str) -> None:
        ...


class LocalArtifactStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write_bytes(
        self,
        *,
        job_id: str,
        artifact_id: str,
        filename: str,
        content: bytes,
        metadata: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
    ) -> StoredArtifactRef:
        target = self._target_path(job_id=job_id, artifact_id=artifact_id, filename=filename)
        target.write_bytes(content)
        return StoredArtifactRef(
            storage_uri=str(target),
            hash=hashlib.sha256(content).hexdigest(),
            size=len(content),
        )

    def copy_path(
        self,
        *,
        job_id: str,
        artifact_id: str,
        filename: str,
        source_path: Path | str,
        metadata: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
    ) -> StoredArtifactRef:
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"Artifact source path does not exist: {source}")
        target = self._target_path(job_id=job_id, artifact_id=artifact_id, filename=filename or source.name)
        if source.is_dir():
            shutil.copytree(source, target)
            digest, size = hash_directory(target)
        else:
            shutil.copy2(source, target)
            content = target.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            size = len(content)
        return StoredArtifactRef(storage_uri=str(target), hash=digest, size=size)

    def read_bytes(self, storage_uri: str) -> bytes:
        return Path(storage_uri).read_bytes()

    def exists(self, storage_uri: str) -> bool:
        return Path(storage_uri).exists()

    def is_file(self, storage_uri: str) -> bool:
        return Path(storage_uri).is_file()

    def is_directory(self, storage_uri: str) -> bool:
        return Path(storage_uri).is_dir()

    def local_path(self, storage_uri: str) -> Path | None:
        return Path(storage_uri)

    def materialize_directory(self, storage_uri: str, target_dir: Path | str) -> Path:
        source = Path(storage_uri)
        if not source.is_dir():
            raise NotADirectoryError(f"Artifact is not a directory: {storage_uri}")
        target = Path(target_dir)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        return target

    def filename(self, storage_uri: str) -> str:
        return Path(storage_uri).name

    def suffix(self, storage_uri: str) -> str:
        return Path(storage_uri).suffix

    def delete(self, storage_uri: str) -> None:
        target = Path(storage_uri)
        root = self.root.resolve()
        resolved = target.resolve()
        if resolved != root and root not in resolved.parents:
            raise ArtifactStoreError(f"Refusing to delete artifact outside store root: {storage_uri}")
        if not target.exists() and not target.is_symlink():
            return
        if target.is_symlink() or target.is_file():
            target.unlink()
            return
        if target.is_dir():
            shutil.rmtree(target)

    def _target_path(self, *, job_id: str, artifact_id: str, filename: str) -> Path:
        job_dir = self.root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        safe_filename = Path(filename).name or "artifact.bin"
        return job_dir / f"{artifact_id}-{safe_filename}"


class S3CompatibleArtifactStore:
    """兼容 S3/MinIO 的 Artifact Store。"""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        client: ObjectStorageClient | None = None,
        presign_ttl_seconds: int = 3600,
    ) -> None:
        if not bucket:
            raise ArtifactStoreConfigurationError("S3-compatible artifact store requires a bucket name.")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = client
        self.presign_ttl_seconds = presign_ttl_seconds

    def write_bytes(
        self,
        *,
        job_id: str,
        artifact_id: str,
        filename: str,
        content: bytes,
        metadata: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
    ) -> StoredArtifactRef:
        key = self._object_key(job_id=job_id, artifact_id=artifact_id, filename=filename)
        self._client().put_object(self.bucket, key, content, metadata=metadata, tags=tags)
        return StoredArtifactRef(
            storage_uri=self._uri(key),
            hash=hashlib.sha256(content).hexdigest(),
            size=len(content),
        )

    def copy_path(
        self,
        *,
        job_id: str,
        artifact_id: str,
        filename: str,
        source_path: Path | str,
        metadata: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
    ) -> StoredArtifactRef:
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"Artifact source path does not exist: {source}")
        if source.is_dir():
            base_key = self._object_key(job_id=job_id, artifact_id=artifact_id, filename=filename).rstrip("/")
            digest, size = hash_directory(source)
            for file_path in sorted(path for path in source.rglob("*") if path.is_file()):
                relative = file_path.relative_to(source).as_posix()
                self._client().put_object(
                    self.bucket,
                    f"{base_key}/{relative}",
                    file_path.read_bytes(),
                    metadata=metadata,
                    tags=tags,
                )
            return StoredArtifactRef(storage_uri=f"{self._uri(base_key)}/", hash=digest, size=size)
        content = source.read_bytes()
        return self.write_bytes(
            job_id=job_id,
            artifact_id=artifact_id,
            filename=filename or source.name,
            content=content,
            metadata=metadata,
            tags=tags,
        )

    def read_bytes(self, storage_uri: str) -> bytes:
        bucket, key = self._parse_uri(storage_uri)
        if storage_uri.endswith("/"):
            raise IsADirectoryError(f"Artifact is a directory prefix: {storage_uri}")
        return self._client().get_object(bucket, key)

    def exists(self, storage_uri: str) -> bool:
        bucket, key = self._parse_uri(storage_uri)
        if storage_uri.endswith("/"):
            return bool(self._client().list_objects(bucket, key.rstrip("/") + "/"))
        return self._client().object_exists(bucket, key)

    def is_file(self, storage_uri: str) -> bool:
        return not storage_uri.endswith("/") and self.exists(storage_uri)

    def is_directory(self, storage_uri: str) -> bool:
        return storage_uri.endswith("/") and self.exists(storage_uri)

    def local_path(self, storage_uri: str) -> Path | None:
        return None

    def materialize_directory(self, storage_uri: str, target_dir: Path | str) -> Path:
        bucket, key = self._parse_uri(storage_uri)
        prefix = key.rstrip("/") + "/"
        target = Path(target_dir)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        object_keys = self._client().list_objects(bucket, prefix)
        if not object_keys:
            raise FileNotFoundError(f"Directory artifact content not found: {storage_uri}")
        for object_key in object_keys:
            relative = PurePosixPath(object_key.removeprefix(prefix))
            if not relative.parts or ".." in relative.parts:
                continue
            output = target.joinpath(*relative.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(self._client().get_object(bucket, object_key))
        return target

    def filename(self, storage_uri: str) -> str:
        _, key = self._parse_uri(storage_uri)
        return PurePosixPath(key.rstrip("/")).name

    def suffix(self, storage_uri: str) -> str:
        return PurePosixPath(self.filename(storage_uri)).suffix

    def delete(self, storage_uri: str) -> None:
        bucket, key = self._parse_uri(storage_uri)
        if storage_uri.endswith("/"):
            for object_key in self._client().list_objects(bucket, key.rstrip("/") + "/"):
                self._client().delete_object(bucket, object_key)
            return
        self._client().delete_object(bucket, key)

    def presigned_read_url(self, storage_uri: str, *, expires_in_seconds: int | None = None) -> str:
        bucket, key = self._parse_uri(storage_uri)
        if storage_uri.endswith("/"):
            raise IsADirectoryError(f"Artifact is a directory prefix: {storage_uri}")
        client = self._client()
        signer = getattr(client, "presigned_get_object_url", None)
        if not callable(signer):
            raise ArtifactStoreConfigurationError("S3-compatible artifact store client does not support signed URLs.")
        return signer(
            bucket,
            key,
            expires_in_seconds=expires_in_seconds or self.presign_ttl_seconds,
        )

    def _client(self) -> ObjectStorageClient:
        if self.client is None:
            raise ArtifactStoreConfigurationError(
                "S3-compatible artifact store requires an ObjectStorageClient adapter; no SDK is bundled."
            )
        return self.client

    def _object_key(self, *, job_id: str, artifact_id: str, filename: str) -> str:
        safe_filename = Path(filename).name or "artifact.bin"
        parts = [part for part in (self.prefix, job_id, f"{artifact_id}-{safe_filename}") if part]
        return "/".join(quote(part, safe="/") for part in parts)

    def _uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def _parse_uri(self, storage_uri: str) -> tuple[str, str]:
        parsed = urlparse(storage_uri)
        if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
            raise ValueError(f"Unsupported S3 artifact URI: {storage_uri}")
        return parsed.netloc, parsed.path.lstrip("/")


class InMemoryObjectStorageClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.metadata: dict[tuple[str, str], dict[str, str]] = {}
        self.tags: dict[tuple[str, str], dict[str, str]] = {}

    def put_object(
        self,
        bucket: str,
        key: str,
        content: bytes,
        *,
        metadata: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.objects[(bucket, key)] = content
        self.metadata[(bucket, key)] = dict(metadata or {})
        self.tags[(bucket, key)] = dict(tags or {})

    def get_object(self, bucket: str, key: str) -> bytes:
        try:
            return self.objects[(bucket, key)]
        except KeyError as error:
            raise FileNotFoundError(f"Object not found: s3://{bucket}/{key}") from error

    def object_exists(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self.objects

    def list_objects(self, bucket: str, prefix: str) -> list[str]:
        return sorted(key for item_bucket, key in self.objects if item_bucket == bucket and key.startswith(prefix))

    def delete_object(self, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)
        self.metadata.pop((bucket, key), None)
        self.tags.pop((bucket, key), None)

    def presigned_get_object_url(self, bucket: str, key: str, *, expires_in_seconds: int) -> str:
        if not self.object_exists(bucket, key):
            raise FileNotFoundError(f"Object not found: s3://{bucket}/{key}")
        return f"https://object-storage.local/{bucket}/{key}?expires={expires_in_seconds}"


def hash_directory(directory: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total_size = 0
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    for file_path in files:
        relative_path = file_path.relative_to(directory).as_posix()
        content = file_path.read_bytes()
        total_size += len(content)
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).hexdigest().encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest(), total_size


def artifact_lifecycle_rules(*, prefix: str, ephemeral_days: int) -> list[dict[str, Any]]:
    if ephemeral_days <= 0:
        raise ArtifactStoreConfigurationError("S3 artifact lifecycle days must be greater than zero.")
    normalized_prefix = prefix.strip("/")
    prefix_filter = f"{normalized_prefix}/" if normalized_prefix else ""
    return [
        {
            "ID": "ai-jsunpack-ephemeral-artifacts",
            "Status": "Enabled",
            "Filter": {
                "And": {
                    "Prefix": prefix_filter,
                    "Tags": [{"Key": "retentionClass", "Value": "ephemeral"}],
                }
            },
            "Expiration": {"Days": ephemeral_days},
        }
    ]


def _normalize_object_metadata(metadata: dict[str, str]) -> dict[str, str]:
    return {key.lower().replace("_", "-"): str(value) for key, value in metadata.items() if value is not None}


def _normalize_object_tags(tags: dict[str, str]) -> dict[str, str]:
    return {str(key): str(value) for key, value in tags.items() if value is not None}


def _object_keys_from_page(page: dict[str, Any]) -> list[str]:
    return [item["Key"] for item in page.get("Contents", []) if "Key" in item]


def _is_missing_object_error(error: Exception) -> bool:
    if isinstance(error, FileNotFoundError):
        return True
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    error_code = str(response.get("Error", {}).get("Code", ""))
    status_code = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return error_code in {"404", "NoSuchKey", "NotFound"} or status_code == 404


def _is_missing_lifecycle_error(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    error_code = str(response.get("Error", {}).get("Code", ""))
    status_code = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return error_code in {"NoSuchLifecycleConfiguration", "404", "NoSuchBucketPolicy"} or status_code == 404
