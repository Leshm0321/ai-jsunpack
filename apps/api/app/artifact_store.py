from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import quote, urlparse


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
    def put_object(self, bucket: str, key: str, content: bytes) -> None:
        ...

    def get_object(self, bucket: str, key: str) -> bytes:
        ...

    def object_exists(self, bucket: str, key: str) -> bool:
        ...

    def list_objects(self, bucket: str, prefix: str) -> list[str]:
        ...

    def delete_object(self, bucket: str, key: str) -> None:
        ...


class ArtifactStore(Protocol):
    def write_bytes(self, *, job_id: str, artifact_id: str, filename: str, content: bytes) -> StoredArtifactRef:
        ...

    def copy_path(self, *, job_id: str, artifact_id: str, filename: str, source_path: Path | str) -> StoredArtifactRef:
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

    def write_bytes(self, *, job_id: str, artifact_id: str, filename: str, content: bytes) -> StoredArtifactRef:
        target = self._target_path(job_id=job_id, artifact_id=artifact_id, filename=filename)
        target.write_bytes(content)
        return StoredArtifactRef(
            storage_uri=str(target),
            hash=hashlib.sha256(content).hexdigest(),
            size=len(content),
        )

    def copy_path(self, *, job_id: str, artifact_id: str, filename: str, source_path: Path | str) -> StoredArtifactRef:
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
    """S3/MinIO-compatible artifact store boundary.

    A real deployment can provide an ObjectStorageClient adapter backed by a chosen SDK.
    This module intentionally avoids adding a network SDK dependency.
    """

    def __init__(self, *, bucket: str, prefix: str = "", client: ObjectStorageClient | None = None) -> None:
        if not bucket:
            raise ArtifactStoreConfigurationError("S3-compatible artifact store requires a bucket name.")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = client

    def write_bytes(self, *, job_id: str, artifact_id: str, filename: str, content: bytes) -> StoredArtifactRef:
        key = self._object_key(job_id=job_id, artifact_id=artifact_id, filename=filename)
        self._client().put_object(self.bucket, key, content)
        return StoredArtifactRef(
            storage_uri=self._uri(key),
            hash=hashlib.sha256(content).hexdigest(),
            size=len(content),
        )

    def copy_path(self, *, job_id: str, artifact_id: str, filename: str, source_path: Path | str) -> StoredArtifactRef:
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"Artifact source path does not exist: {source}")
        if source.is_dir():
            base_key = self._object_key(job_id=job_id, artifact_id=artifact_id, filename=filename).rstrip("/")
            digest, size = hash_directory(source)
            for file_path in sorted(path for path in source.rglob("*") if path.is_file()):
                relative = file_path.relative_to(source).as_posix()
                self._client().put_object(self.bucket, f"{base_key}/{relative}", file_path.read_bytes())
            return StoredArtifactRef(storage_uri=f"{self._uri(base_key)}/", hash=digest, size=size)
        content = source.read_bytes()
        return self.write_bytes(job_id=job_id, artifact_id=artifact_id, filename=filename or source.name, content=content)

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

    def put_object(self, bucket: str, key: str, content: bytes) -> None:
        self.objects[(bucket, key)] = content

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
