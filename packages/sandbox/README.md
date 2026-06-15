# Sandbox Package

This package hosts the local controlled runner interface used by Worker build, typecheck, install, and runtime validation stages.

Required runner behavior:

- Use a per-attempt temporary directory.
- Clear inherited credentials and sensitive environment variables.
- Default to no network.
- Enforce command allowlists, timeouts, and output limits.
- Capture stdout, stderr, exit code, duration, resource usage, and failure class.

Current implementation:

- `LocalSandboxRunner` provides a per-attempt temporary workspace and argv-only subprocess execution.
- `SandboxPolicy` defines command allowlists, timeout, output limit, environment allowlist, and `network_policy`.
- The local runner records the network policy contract but does not provide OS-level network isolation yet.
