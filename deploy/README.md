# Deployment Profiles

This directory separates runtime configuration by service boundary.

- `api` owns HTTP, auth, metadata, and Artifact access. It must not receive sandbox, browser-runner, Core CLI, or model provider credentials.
- `worker` owns Core CLI, Agent runtime, build/typecheck sandbox execution, and packaging.
- `browser-runner` owns Playwright/browser execution capacity for deployments that split browser work from the main worker pool.
- `db` and `artifact-store` are infrastructure services shared by API and Worker.
- `web` only receives `VITE_API_*` values at build/runtime boundary.

The compose file is a deployment contract and local starting point. Replace placeholder image names with your built images or CI-published images before production use.

## Validation

When `AI_JSUNPACK_SERVICE_ROLE=api` is set, the API process validates its environment during import/startup and fails fast if Worker/browser execution configuration is present. Without an explicit service role, local development stays permissive and `/health` reports a warning profile instead of failing.
