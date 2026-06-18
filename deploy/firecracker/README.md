# Firecracker Launcher Deployment

This directory contains the production launcher template for the Worker Firecracker sandbox adapter.

The Worker does not start Firecracker directly. `packages.sandbox.FirecrackerSandboxRunner` sends a JSON request to the command configured by `AI_JSUNPACK_FIRECRACKER_RUNNER_COMMAND` or `buildValidation.firecrackerRunnerCommand`. `launcher.py` validates that request, prepares a microVM exchange directory, and delegates actual KVM/jailer execution to a deployment-provided wrapper command.

## Host Prerequisites

- Linux host with `/dev/kvm` available to the launcher process.
- Firecracker and firecracker-jailer installed and pinned to the deployed version.
- Prepared guest kernel image and rootfs image.
- Guest rootfs includes Node.js/npm or the exact build/typecheck toolchain required by generated projects.
- A non-root service user and jailer workspace with restricted permissions.
- Artifact Store access path for moving workspace inputs and evidence outputs across the VM boundary.

## Worker Configuration

Example:

```bash
AI_JSUNPACK_SANDBOX_RUNNER=firecracker
AI_JSUNPACK_SANDBOX_RUNTIME_NAME=firecracker
AI_JSUNPACK_SANDBOX_RUNTIME_VERSION=1.9.1
AI_JSUNPACK_FIRECRACKER_RUNNER_COMMAND="/usr/local/bin/ai-jsunpack-firecracker-launcher --kernel /srv/ai-jsunpack/firecracker/vmlinux --rootfs /srv/ai-jsunpack/firecracker/rootfs.ext4 --jailer /usr/bin/firecracker-jailer --firecracker /usr/bin/firecracker --socket-dir /run/ai-jsunpack/firecracker --wrapper-command /usr/local/bin/ai-jsunpack-firecracker-wrapper"
```

`worker.env.example` keeps this value empty because local development and CI usually do not have KVM, kernel, rootfs, and jailer assets.

## Request Protocol

The Worker writes one JSON object to launcher stdin:

```json
{
  "version": 1,
  "runnerKind": "firecracker",
  "workspace": "/tmp/ai-jsunpack-sandbox-abcd",
  "workingDirectory": "project",
  "command": ["npm", "run", "--ignore-scripts", "build"],
  "stdinBase64": null,
  "environment": {"PATH": "/usr/local/bin:/usr/bin"},
  "timeoutMs": 120000,
  "outputLimitBytes": 131072,
  "networkPolicy": "deny",
  "resourcePolicy": {
    "runnerKind": "firecracker",
    "enforcement": "runtime_isolated",
    "processLimit": 64,
    "cpuTimeLimitMs": 120000,
    "memoryLimitBytes": 536870912
  }
}
```

Security requirements:

- `workspace` must be an existing directory created by the Worker attempt.
- `workingDirectory` must be relative and must not escape `workspace`.
- `environment` is already cleaned by the Worker; the launcher should pass only these values to the guest.
- `networkPolicy=deny` is the default and must create a no-egress guest.
- `networkPolicy=allow` must be explicitly mapped by deployment policy, for example through a dedicated tap device and egress allowlist.

## Wrapper Contract

`launcher.py` passes a control document to `--wrapper-command` stdin. The wrapper owns the deployment-specific Firecracker steps:

- create jailer workspace and Firecracker API socket;
- create or attach the guest rootfs copy/overlay;
- copy or mount the attempt workspace into the guest boundary;
- start Firecracker with the configured kernel/rootfs;
- execute the requested command inside the guest;
- copy stdout/stderr and output artifacts back to the launcher exchange directory;
- stop the microVM and clean temporary resources.

The wrapper may either print the final launcher response JSON directly, or print plain stdout/stderr and rely on the launcher to classify the wrapper exit code as `unknown` on failure.

Preferred wrapper response:

```json
{
  "stdout": "build output",
  "stderr": "",
  "exitCode": 0,
  "timedOut": false,
  "outputTruncated": false,
  "failureClass": "none"
}
```

Allowed `failureClass` values are the shared project failure classes, including `sandbox_denied`, `timeout`, `resource_limit`, `dependency_missing`, `install_failed`, `build_error`, `type_error`, and `unknown`.

## Resource Mapping

- `processLimit`: map to guest init policy, cgroup pids limit, or jailer/cgroup configuration.
- `cpuTimeLimitMs`: map to wrapper timeout plus guest/cgroup CPU controls where available.
- `memoryLimitBytes`: map to Firecracker machine memory and host cgroup memory limit.
- `timeoutMs`: the launcher kills the wrapper after this wall-clock timeout; the wrapper should also enforce a guest-side timeout.
- `outputLimitBytes`: launcher truncates stdout/stderr to keep evidence bounded.

If a requested control cannot be enforced, the wrapper must either reject with `sandbox_denied` or include the limitation in stderr and return an auditable failure. It must not silently fall back to local execution.

## Artifact Store Exchange

For production deployments, prefer Artifact Store mediated exchange over host-wide shared paths:

- Worker materializes the generated project into the attempt workspace.
- Launcher copies only that workspace into a per-run exchange directory or uploads it to an object-store prefix.
- Guest writes result logs and generated evidence to the exchange directory or object-store prefix.
- Launcher returns stdout/stderr and exit metadata to Worker; Worker persists `build_log`, `build_artifact`, and review evidence through the normal Artifact Store.

The template does not embed object-store credentials. If the wrapper needs object-store access, scope credentials to the per-run prefix and keep them out of API and Web service environments.

## Deployment Smoke Test

Protocol-only dry run:

```bash
printf '%s' '{"version":1,"runnerKind":"firecracker","workspace":"/tmp/work","workingDirectory":".","command":["node","--version"],"stdinBase64":null,"environment":{},"timeoutMs":1000,"outputLimitBytes":4096,"networkPolicy":"deny","resourcePolicy":{"runnerKind":"firecracker"}}' \
  | /usr/local/bin/ai-jsunpack-firecracker-launcher \
      --kernel /srv/ai-jsunpack/firecracker/vmlinux \
      --rootfs /srv/ai-jsunpack/firecracker/rootfs.ext4 \
      --jailer /usr/bin/firecracker-jailer \
      --firecracker /usr/bin/firecracker \
      --dry-run
```

Production acceptance checks:

- Missing kernel/rootfs/jailer/firecracker returns `sandbox_denied`, not local fallback.
- `workingDirectory=..` returns `sandbox_denied`.
- `networkPolicy=deny` produces a guest with no egress.
- A command exceeding `timeoutMs` returns `failureClass=timeout`.
- Excessive output sets `outputTruncated=true`.
- Non-zero build command returns the command-specific failure class from Worker evidence.
- `build_artifact.resourcePolicy.runnerKind` is `firecracker` and `enforcement` is `runtime_isolated`.
- Worker reports retain `build_log`, `build_artifact`, Review evidence, and result package lineage.
