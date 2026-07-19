from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import time
from typing import Any, Literal

from fastapi import Header, HTTPException


AUTH_SECRET_ENV = "AI_JSUNPACK_AUTH_SECRET"
TOKEN_TYPE = "AI-JSUNPACK-AUTH"
TOKEN_ALGORITHM = "HS256"
SERVICE_ROLE_WORKER = "worker"

ProjectRole = Literal["viewer", "maintainer", "owner"]
TokenKind = Literal["user", "service"]

ROLE_RANK: dict[ProjectRole, int] = {
    "viewer": 1,
    "maintainer": 2,
    "owner": 3,
}


class AuthError(ValueError):
    pass


class AuthConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccessContext:
    subject: str
    kind: TokenKind
    projects: dict[str, ProjectRole]
    service_roles: tuple[str, ...]

    def role_for(self, project_id: str) -> ProjectRole | None:
        return self.projects.get(project_id)

    def has_project_role(self, project_id: str, minimum_role: ProjectRole) -> bool:
        role = self.role_for(project_id)
        return role is not None and ROLE_RANK[role] >= ROLE_RANK[minimum_role]

    def has_service_role(self, service_role: str) -> bool:
        return service_role in self.service_roles


def require_access(authorization: str | None = Header(default=None)) -> AccessContext:
    try:
        return verify_authorization_header(authorization)
    except AuthConfigurationError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    except AuthError as error:
        raise HTTPException(
            status_code=401,
            detail=str(error),
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


def verify_authorization_header(authorization: str | None) -> AccessContext:
    if authorization is None or not authorization.strip():
        raise AuthError("缺少 Bearer token")
    scheme, separator, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        raise AuthError("授权方案无效")
    return verify_auth_token(token.strip())


def verify_auth_token(token: str, *, secret: str | None = None, now: int | None = None) -> AccessContext:
    configured_secret = secret if secret is not None else auth_secret()
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise AuthError("Bearer token 格式无效")

    header_b64, payload_b64, signature_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}"
    expected_signature = _base64url_encode(
        hmac.new(configured_secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(signature_b64, expected_signature):
        raise AuthError("Bearer token 签名无效")

    header = _decode_json_object(header_b64, "token header")
    payload = _decode_json_object(payload_b64, "token payload")
    if header.get("alg") != TOKEN_ALGORITHM or header.get("typ") != TOKEN_TYPE:
        raise AuthError("不支持该 Bearer token header")

    subject = _required_string(payload, "sub")
    kind = payload.get("kind")
    if kind not in ("user", "service"):
        raise AuthError("Bearer token type 无效")
    expires_at = payload.get("exp")
    if not isinstance(expires_at, int):
        raise AuthError("Bearer token expiration 无效")
    if (now if now is not None else int(time.time())) >= expires_at:
        raise AuthError("Bearer token 已过期")

    return AccessContext(
        subject=subject,
        kind=kind,
        projects=_project_roles(payload.get("projects")),
        service_roles=_service_roles(payload.get("serviceRoles")),
    )


def create_auth_token(
    *,
    subject: str,
    projects: dict[str, ProjectRole],
    secret: str | None = None,
    kind: TokenKind = "user",
    service_roles: list[str] | tuple[str, ...] | None = None,
    ttl_seconds: int = 3600,
    expires_at: int | None = None,
) -> str:
    configured_secret = secret if secret is not None else auth_secret()
    header = {"alg": TOKEN_ALGORITHM, "typ": TOKEN_TYPE}
    payload: dict[str, Any] = {
        "sub": subject,
        "kind": kind,
        "exp": int(expires_at if expires_at is not None else time.time() + ttl_seconds),
        "projects": projects,
    }
    if service_roles:
        payload["serviceRoles"] = list(service_roles)
    header_b64 = _base64url_json(header)
    payload_b64 = _base64url_json(payload)
    signing_input = f"{header_b64}.{payload_b64}"
    signature_b64 = _base64url_encode(
        hmac.new(configured_secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{signing_input}.{signature_b64}"


def auth_secret() -> str:
    secret = os.getenv(AUTH_SECRET_ENV)
    if not secret:
        raise AuthConfigurationError(f"未配置环境变量 {AUTH_SECRET_ENV}")
    return secret


def _project_roles(value: Any) -> dict[str, ProjectRole]:
    if not isinstance(value, dict):
        raise AuthError("项目成员关系声明无效")
    projects: dict[str, ProjectRole] = {}
    for project_id, role in value.items():
        if not isinstance(project_id, str) or not project_id.strip():
            raise AuthError("项目成员关系中的项目 ID 无效")
        if role not in ROLE_RANK:
            raise AuthError("项目成员关系中的角色无效")
        projects[project_id] = role
    return projects


def _service_roles(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(role, str) and role for role in value):
        raise AuthError("服务角色声明无效")
    return tuple(value)


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AuthError(f"Bearer token 字段 {key} 无效")
    return value


def _base64url_json(value: dict[str, Any]) -> str:
    return _base64url_encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_json_object(value: str, label: str) -> dict[str, Any]:
    try:
        padded = value + ("=" * ((4 - len(value) % 4) % 4))
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        parsed = json.loads(decoded)
    except Exception as error:
        raise AuthError(f"{label}无效") from error
    if not isinstance(parsed, dict):
        raise AuthError(f"{label}无效")
    return parsed
