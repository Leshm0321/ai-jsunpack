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

## Sandbox and Browser Isolation Profiles

`build_artifact.resourcePolicy` is the audit contract for execution isolation. It records `enforcement`, `runnerKind`, runtime metadata, capability status, and known limitations for every build/typecheck validation.

Supported runner profiles:

| runnerKind | enforcement | Current execution behavior | Audit meaning |
| --- | --- | --- | --- |
| `local` | `local_best_effort` | Executes in a temporary local workspace with command allowlist and cleaned environment. | Records policy intent; no OS/container isolation is claimed. |
| `container` | `container_enforced` | Executes through Docker or Podman when available. | Records Docker/Podman network, process, memory, CPU, filesystem capability differences. |
| `gvisor` | `runtime_isolated` | Audit-only profile in this Worker; command execution is denied until a runsc adapter is wired. | Use when deployment routes container execution through gVisor/runsc and wants that boundary reflected in evidence. |
| `firecracker` | `runtime_isolated` | Audit-only profile in this Worker; command execution is denied until a microVM adapter is wired. | Use when deployment owns Firecracker/KVM/jailer/rootfs setup and Artifact Store exchange across the VM boundary. |
| `remote_browser_runner` | `remote_isolated` | Audit profile for browser/runtime validation isolation, not Worker build/typecheck command execution. | Use when Playwright/browser work is delegated to a separate Browser Runner service with its own auth, egress, and artifact exchange controls. |

The high-isolation profiles intentionally do not fall back to a weaker runner. If `AI_JSUNPACK_SANDBOX_RUNNER=gvisor`, `firecracker`, or `remote_browser_runner` is set before a real adapter exists, validation emits `sandbox_denied` evidence with the selected profile and adapter limitation. This keeps audit output honest.

Production guidance:

- Use `container` for the current executable deployment path.
- Use `gvisor` only when Docker/containerd/Kubernetes is configured to use runsc and the Worker adapter can prove it in evidence.
- Use `firecracker` only on Linux hosts with KVM, jailer/rootfs provisioning, explicit resource limits, and Artifact Store transfer across the microVM boundary.
- Use the `browser-runner` service boundary for Playwright/browser execution isolation; pin Playwright client/server versions and protect websocket/API credentials.
