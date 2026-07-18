from __future__ import annotations

import json
import os
from pathlib import Path

from apps.api.app.auth import create_auth_token


AUTH_SECRET_ENV = "AI_JSUNPACK_AUTH_SECRET"
OUTPUT_DIR_ENV = "AI_JSUNPACK_AUTH_OUTPUT_DIR"
WEB_TOKEN_TTL_ENV = "AI_JSUNPACK_WEB_TOKEN_TTL_SECONDS"
SERVICE_TOKEN_TTL_ENV = "AI_JSUNPACK_SERVICE_TOKEN_TTL_SECONDS"


def generate_tokens(output_dir: Path, *, secret: str, web_ttl_seconds: int, service_ttl_seconds: int) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    tokens = {
        "web-token": create_auth_token(
            subject="docker-local-user",
            projects={"default": "owner"},
            secret=secret,
            ttl_seconds=web_ttl_seconds,
        ),
        "browser-runner-token": create_auth_token(
            subject="docker-worker",
            kind="service",
            projects={"default": "owner"},
            service_roles=["worker"],
            secret=secret,
            ttl_seconds=service_ttl_seconds,
        ),
    }
    paths: dict[str, str] = {}
    for name, token in tokens.items():
        path = output_dir / name
        temporary = path.with_suffix(".tmp")
        temporary.write_text(f"{token}\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        paths[name] = str(path)
    return paths


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def main() -> int:
    secret = os.getenv(AUTH_SECRET_ENV, "").strip()
    if not secret:
        raise RuntimeError(f"{AUTH_SECRET_ENV} is required")
    output_dir = Path(os.getenv(OUTPUT_DIR_ENV, "/run/ai-jsunpack-auth"))
    paths = generate_tokens(
        output_dir,
        secret=secret,
        web_ttl_seconds=_positive_int_env(WEB_TOKEN_TTL_ENV, 86_400),
        service_ttl_seconds=_positive_int_env(SERVICE_TOKEN_TTL_ENV, 86_400),
    )
    print(json.dumps({"status": "ok", "tokenFiles": paths}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
