# Sandbox Package

This package will host isolated build, typecheck, install, and browser runtime runner interfaces.

Required runner behavior:

- Use a per-attempt temporary directory.
- Clear inherited credentials and sensitive environment variables.
- Default to no network.
- Enforce command allowlists, timeouts, and output limits.
- Capture stdout, stderr, exit code, duration, resource usage, and failure class.

