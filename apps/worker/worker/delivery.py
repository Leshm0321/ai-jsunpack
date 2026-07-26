from __future__ import annotations

import io
import json
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from apps.api.app.models import ArtifactRecord, JobRecord


USER_INPUT_KINDS = {"single_script", "archive", "folder"}


class DeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryResult:
    artifact: ArtifactRecord | None
    delivery_kind: str
    input_kind: str
    input_name: str
    transformed_files: list[str]
    downgrade_reason: str | None = None


class DeliveryPublisher:
    """Publishes only the reconstructed files intended for the end user."""

    def publish(
        self,
        *,
        job: JobRecord,
        store,
        generated_project: ArtifactRecord | None,
        parent_artifact_ids: list[str],
    ) -> DeliveryResult:
        if generated_project is None:
            return self._unavailable_result(job=job, store=store)

        with self._project_path(store=store, artifact=generated_project) as project_path:
            manifest = self._manifest(project_path)
            input_kind = self._input_kind(job=job, manifest=manifest)
            input_name = self._input_name(job=job, store=store)
            transform_map = self._transform_map(manifest)
            transformed_files = sorted(transform_map)

            downgrade_reason = self._single_file_downgrade_reason(
                input_kind=input_kind,
                manifest=manifest,
                transform_map=transform_map,
            )
            if input_kind == "single_script" and downgrade_reason is None:
                source_path, transformed_path = next(iter(transform_map.items()))
                content = self._safe_project_file(project_path, transformed_path).read_bytes()
                filename = self._single_result_filename(input_name=input_name, source_path=source_path)
                artifact = store.write_artifact(
                    job.id,
                    kind="result_file",
                    stage="packaging",
                    filename=filename,
                    content=content,
                    content_type=self._javascript_content_type(filename),
                    producer="worker.delivery",
                    parent_artifact_ids=[*parent_artifact_ids, generated_project.id],
                    **self._retention_kwargs(job),
                )
                return DeliveryResult(
                    artifact=artifact,
                    delivery_kind="single_file",
                    input_kind=input_kind,
                    input_name=input_name,
                    transformed_files=transformed_files,
                )

            package_bytes = self._project_package_bytes(
                project_path=project_path,
                transform_map=transform_map,
            )
            artifact = store.write_artifact(
                job.id,
                kind="result_package",
                stage="packaging",
                filename=self._package_filename(input_name),
                content=package_bytes,
                content_type="application/zip",
                producer="worker.delivery",
                parent_artifact_ids=[*parent_artifact_ids, generated_project.id],
                **self._retention_kwargs(job),
            )
            return DeliveryResult(
                artifact=artifact,
                delivery_kind="project_package",
                input_kind=input_kind,
                input_name=input_name,
                transformed_files=transformed_files,
                downgrade_reason=downgrade_reason,
            )

    def _unavailable_result(self, *, job: JobRecord, store) -> DeliveryResult:
        input_name = self._input_name(job=job, store=store)
        return DeliveryResult(
            artifact=None,
            delivery_kind="unavailable",
            input_kind=self._input_kind(job=job, manifest={}),
            input_name=input_name,
            transformed_files=[],
            downgrade_reason="Generated project artifact is missing; no user-deliverable result was published.",
        )

    @contextmanager
    def _project_path(self, *, store, artifact: ArtifactRecord) -> Iterator[Path]:
        local_path = store.artifact_local_path(artifact)
        if local_path is not None and local_path.is_dir():
            yield local_path
            return
        with tempfile.TemporaryDirectory(prefix="ai-jsunpack-delivery-") as temp_dir:
            yield store.materialize_artifact_directory(artifact, Path(temp_dir) / "generated-project")

    def _manifest(self, project_path: Path) -> dict[str, Any]:
        manifest_path = project_path / "src" / "reconstruction-manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DeliveryError(f"Generated project manifest is unavailable: {error}") from error
        if not isinstance(payload, dict):
            raise DeliveryError("Generated project manifest must be a JSON object.")
        return payload

    def _input_kind(self, *, job: JobRecord, manifest: dict[str, Any]) -> str:
        job_kind = getattr(job, "input_kind", None)
        if job_kind in USER_INPUT_KINDS:
            return str(job_kind)
        manifest_kind = manifest.get("sourceKind")
        if manifest_kind in USER_INPUT_KINDS:
            return str(manifest_kind)
        return "archive"

    def _input_name(self, *, job: JobRecord, store) -> str:
        configured = getattr(job, "input_name", None)
        if isinstance(configured, str) and configured.strip():
            return Path(configured).name
        artifact_id = getattr(job, "input_artifact_id", None)
        artifact = store.get_artifact(job.id, artifact_id) if artifact_id else None
        if artifact is None:
            return "reconstructed-project"
        filename = getattr(artifact, "filename", None) or store.artifact_filename(artifact)
        prefix = f"{artifact.id}-"
        if filename.startswith(prefix):
            filename = filename.removeprefix(prefix)
        return Path(filename).name or "reconstructed-project"

    def _transform_map(self, manifest: dict[str, Any]) -> dict[str, str]:
        raw_map = manifest.get("sourceTransformMap")
        if isinstance(raw_map, dict):
            pairs = {
                self._safe_relative(str(source)): self._safe_relative(str(target))
                for source, target in raw_map.items()
                if isinstance(source, str) and isinstance(target, str)
            }
            if pairs:
                return dict(sorted(pairs.items()))
        transformed_files = manifest.get("transformedSourceFiles")
        if not isinstance(transformed_files, list):
            return {}
        prefix = "src/transformed/"
        pairs: dict[str, str] = {}
        for raw_path in transformed_files:
            if not isinstance(raw_path, str) or not raw_path.startswith(prefix):
                continue
            target = self._safe_relative(raw_path)
            source = self._safe_relative(raw_path.removeprefix(prefix))
            pairs[source] = target
        return dict(sorted(pairs.items()))

    def _single_file_downgrade_reason(
        self,
        *,
        input_kind: str,
        manifest: dict[str, Any],
        transform_map: dict[str, str],
    ) -> str | None:
        if input_kind != "single_script":
            return None
        placeholders = manifest.get("dependencyPlaceholders")
        if isinstance(placeholders, list) and placeholders:
            return "The script depends on auxiliary modules that cannot be embedded safely; a runnable project ZIP was returned."
        if len(transform_map) != 1:
            return "The input produced multiple transformed scripts, so a runnable project ZIP was returned."
        return None

    def _project_package_bytes(self, *, project_path: Path, transform_map: dict[str, str]) -> bytes:
        source_root = project_path / "public" / "original"
        if not source_root.is_dir():
            raise DeliveryError("Generated project does not contain public/original.")
        entries: dict[str, bytes] = {}
        for file_path in sorted(path for path in source_root.rglob("*") if path.is_file()):
            relative = self._safe_relative(file_path.relative_to(source_root).as_posix())
            entries[relative] = file_path.read_bytes()
        for source_path, transformed_path in transform_map.items():
            entries[source_path] = self._safe_project_file(project_path, transformed_path).read_bytes()

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative, content in sorted(entries.items()):
                archive.writestr(relative, content)
        return buffer.getvalue()

    def _safe_project_file(self, project_path: Path, relative_path: str) -> Path:
        safe_relative = self._safe_relative(relative_path)
        target = project_path.joinpath(*PurePosixPath(safe_relative).parts)
        resolved_root = project_path.resolve()
        resolved_target = target.resolve()
        if resolved_root not in resolved_target.parents:
            raise DeliveryError(f"Generated project path escapes its root: {relative_path}")
        if not target.is_file():
            raise DeliveryError(f"Generated project file is missing: {relative_path}")
        return target

    def _safe_relative(self, value: str) -> str:
        if not value or "\x00" in value:
            raise DeliveryError(f"Unsafe delivery path: {value}")
        if value.startswith(("/", "\\")) or value.startswith("//") or value.startswith("\\\\"):
            raise DeliveryError(f"Unsafe delivery path: {value}")
        if ":" in value:
            raise DeliveryError(f"Unsafe delivery path: {value}")
        normalized = value.replace("\\", "/")
        raw_parts = normalized.split("/")
        if any(part in {"", ".", ".."} for part in raw_parts):
            raise DeliveryError(f"Unsafe delivery path: {value}")
        path = PurePosixPath(normalized)
        if path.is_absolute():
            raise DeliveryError(f"Unsafe delivery path: {value}")
        return path.as_posix()

    def _single_result_filename(self, *, input_name: str, source_path: str) -> str:
        candidate = Path(input_name).name
        if Path(candidate).suffix.lower() not in {".js", ".mjs", ".cjs"}:
            candidate = Path(source_path).name
        return candidate or "transformed.js"

    def _package_filename(self, input_name: str) -> str:
        name = Path(input_name).name
        lower = name.lower()
        for suffix in (".tar.gz", ".tgz", ".zip", ".tar"):
            if lower.endswith(suffix):
                name = name[: -len(suffix)]
                break
        else:
            name = Path(name).stem
        return f"{name or 'reconstructed-project'}-result.zip"

    def _javascript_content_type(self, filename: str) -> str:
        return "text/javascript; charset=utf-8" if filename.lower().endswith(".js") else "application/javascript"

    def _retention_kwargs(self, job: JobRecord) -> dict[str, Any]:
        expires_at = getattr(job, "files_expires_at", None)
        return {"retention_class": "project", "expires_at": expires_at} if expires_at else {"retention_class": "project"}
