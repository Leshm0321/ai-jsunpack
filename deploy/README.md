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

Run the local production smoke/soak acceptance check before release handoff:

```powershell
.venv\Scripts\python.exe -m apps.api.app.deployment_smoke `
  --output tmp\deployment-smoke.json
```

The default path uses temporary SQLite, a temporary Artifact Store, API TestClient, a controlled Worker pipeline, synthetic Browser Runner soak, simulated webhook delivery, and retention cleanup checks. It exits non-zero when any critical check fails. For production-like capacity runs, pass a shared metadata DB and retain artifacts:

```powershell
.venv\Scripts\python.exe -m apps.api.app.deployment_smoke `
  --database-url "postgresql+psycopg://user:pass@db:5432/ai_jsunpack" `
  --artifact-root tmp\deployment-smoke-artifacts `
  --soak-instances 4 `
  --soak-workers-per-instance 2 `
  --soak-runs 200 `
  --output tmp\deployment-smoke-postgres.json
```

## Sandbox and Browser Isolation Profiles

`build_artifact.resourcePolicy` is the audit contract for execution isolation. It records `enforcement`, `runnerKind`, runtime metadata, capability status, and known limitations for every build/typecheck validation.

Supported runner profiles:

| runnerKind | enforcement | Current execution behavior | Audit meaning |
| --- | --- | --- | --- |
| `local` | `local_best_effort` | Executes in a temporary local workspace with command allowlist and cleaned environment. | Records policy intent; no OS/container isolation is claimed. |
| `container` | `container_enforced` | Executes through Docker or Podman when available. | Records Docker/Podman network, process, memory, CPU, filesystem capability differences. |
| `gvisor` | `runtime_isolated` | Executes build/typecheck through Docker or Podman with `--runtime runsc` when a container runtime is configured. | Use when deployment routes container execution through gVisor/runsc and wants that boundary reflected in evidence. |
| `firecracker` | `runtime_isolated` | Executes through a deployment-provided Firecracker launcher when `AI_JSUNPACK_FIRECRACKER_RUNNER_COMMAND` or `buildValidation.firecrackerRunnerCommand` is configured; otherwise execution is denied. | Use when deployment owns Firecracker/KVM/jailer/rootfs setup and Artifact Store exchange across the VM boundary. |
| `remote_browser_runner` | `remote_isolated` | Executes runtime smoke/compare through the separate Browser Runner service when `AI_JSUNPACK_BROWSER_RUNNER_URL` is configured; it does not execute Worker build/typecheck commands. | Use when Playwright/browser work is delegated to a separate Browser Runner service with its own auth, egress, and artifact exchange controls. |

The high-isolation build profiles intentionally do not fall back to a weaker runner. If `AI_JSUNPACK_SANDBOX_RUNNER=gvisor` is set and no Docker/Podman runtime can be found or configured, validation emits `sandbox_denied` evidence with the selected profile and adapter limitation. If `AI_JSUNPACK_SANDBOX_RUNNER=firecracker` is set without a launcher command, validation emits `sandbox_denied` evidence instead of running locally. `remote_browser_runner` is executable for browser validation only; build/typecheck still require `local`, `container`, `gvisor`, or configured `firecracker`.

Production guidance:

- Use `container` for the current executable deployment path.
- Use `gvisor` only when Docker or Podman is configured with the `runsc` runtime. Worker invokes the configured container runtime with `--runtime runsc`, the same workspace/image/env-cleaning behavior as the container runner, and records `runtime_isolated`, `runnerKind=gvisor`, capability details, runtime version, and limitations in `build_artifact.resourcePolicy`.
- Use `firecracker` only on Linux hosts with KVM, jailer/rootfs provisioning, explicit resource limits, and Artifact Store transfer across the microVM boundary. The configured launcher receives a JSON request on stdin with `workspace`, `workingDirectory`, `command`, `environment`, `networkPolicy`, `resourcePolicy`, `timeoutMs`, and optional `stdinBase64`; it must print JSON on stdout with `stdout`, `stderr`, `exitCode`, `timedOut`, `outputTruncated`, and `failureClass`.
- `deploy/firecracker/launcher.py` is the production launcher template. It validates the Worker protocol, prepares a per-run exchange directory, checks kernel/rootfs/jailer/firecracker prerequisites, and delegates actual KVM/jailer execution to a deployment wrapper command. `deploy/firecracker/README.md` defines the deployment acceptance checklist, resource mapping, network isolation requirements, Artifact Store exchange boundary, and JSON request/response contract.
- Use the `browser-runner` service boundary for Playwright/browser execution isolation. Worker submits asynchronous `/browser-runs` requests with a signed worker service Bearer token, polls completion, and records `executionBoundary` plus runtime trace/screenshot evidence in the result package.
- The browser-runner ASGI app is `apps.browser_runner.app.main:app`; deploy it with the same `AI_JSUNPACK_AUTH_SECRET` as Worker and install Playwright browsers in that image.
- The browser-runner queue is selected with `AI_JSUNPACK_BROWSER_RUNNER_QUEUE_BACKEND`. Use `postgresql` with `AI_JSUNPACK_BROWSER_RUNNER_QUEUE_DATABASE_URL` for multi-instance deployments that share the metadata DB; use `sqlite` with `AI_JSUNPACK_BROWSER_RUNNER_DB_PATH` only for single-instance local operation.
- `AI_JSUNPACK_BROWSER_RUNNER_WORKERS`, `AI_JSUNPACK_BROWSER_RUNNER_MAX_ATTEMPTS`, `AI_JSUNPACK_BROWSER_RUNNER_LEASE_SECONDS`, `AI_JSUNPACK_BROWSER_RUNNER_RETRY_BACKOFF_SECONDS`, and `AI_JSUNPACK_BROWSER_RUNNER_POLL_SECONDS` control per-instance concurrency, retries, lease recovery, and scheduling cadence.
- `AI_JSUNPACK_BROWSER_RUNNER_MAX_QUEUE_AGE_MS`, `AI_JSUNPACK_BROWSER_RUNNER_MAX_CLAIM_LATENCY_MS`, `AI_JSUNPACK_BROWSER_RUNNER_MAX_EXPIRED_RUNNING`, and `AI_JSUNPACK_BROWSER_RUNNER_MAX_RETRY_RATE` define the service-local health thresholds used by `/health`, `/browser-runs/metrics`, and audit evidence.
- Queue recovery is best-effort on service start: expired `running` runs are re-queued until their attempt cap is reached, then emitted as `best_effort` evidence with timeout classification.
- `/health` returns `BrowserRunnerQueueHealth` with backend status, queue metrics, worker settings, and alerts; use it as the container readiness/liveness check. `/browser-runs/metrics` requires a worker service Bearer token and returns the same queue metrics without the health wrapper.
- The API exposes `/ops/heartbeats`, `/ops/metrics`, and `/ops/alerts` JSON endpoints for shared heartbeat persistence, aggregated ops snapshots, and best-effort alert webhook delivery.
- The API also exposes `/ops/prometheus` as the Prometheus scrape surface for the same aggregated ops snapshot. Scrape requests must include a Bearer token with ops read access; the endpoint intentionally does not provide anonymous metrics because service instance, queue, job status, and alert labels are operationally sensitive.
- `AI_JSUNPACK_OPS_HEARTBEAT_TTL_SECONDS` controls heartbeat expiry for API, Worker, and Browser Runner ops records; `AI_JSUNPACK_ALERT_WEBHOOK_URL` and `AI_JSUNPACK_ALERT_WEBHOOK_TIMEOUT_SECONDS` control API alert delivery.
- Monitor `queuedCount`, `oldestQueuedAgeMs`, `claimLatencyMs`, `averageRunDurationMs`, `retryRate`, `leaseRecoveryCount`, `expiredRunningCount`, and `backendStatus` for each Browser Runner deployment. Alert when backend status is degraded, `expiredRunningCount` is non-zero, queue age or claim latency exceeds the configured thresholds, retry rate rises above the configured threshold, or queued runs stay above total worker capacity.
- `BrowserRunSummary` and `runtime_trace.executionBoundary` record queue backend, run attempt, max attempts, worker id, lease recovery, retry policy, queue length, claim latency, run duration, retry rate, backend health, and alert fields so multi-instance scheduling remains auditable in result packages.
