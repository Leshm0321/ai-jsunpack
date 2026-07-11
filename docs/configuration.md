# Configuration

AI JS Unpack supports JSON and YAML startup configuration. Environment variables remain available for container overrides and secret injection, but they are no longer the primary way to describe ordinary service settings.

## Quick Start

Copy either committed example and adjust the non-secret values:

```powershell
Copy-Item config/ai-jsunpack.example.yaml config/ai-jsunpack.yaml
```

Validate the file before starting services:

```powershell
.venv\Scripts\python.exe -m packages.configuration validate config/ai-jsunpack.yaml
.venv\Scripts\python.exe -m packages.configuration print-effective config/ai-jsunpack.yaml
```

Start a service with the same configuration file:

```powershell
node scripts/dev.mjs api --config config/ai-jsunpack.yaml
node scripts/dev.mjs web --config config/ai-jsunpack.yaml
node scripts/dev.mjs worker --config config/ai-jsunpack.yaml
node scripts/dev.mjs browser-runner --config config/ai-jsunpack.yaml
```

The equivalent environment entry point is:

```powershell
$env:AI_JSUNPACK_CONFIG_FILE = "config/ai-jsunpack.yaml"
```

## Precedence

Startup values resolve in this order:

```text
built-in defaults < JSON/YAML file < environment variables < explicit CLI arguments
```

Runtime application settings resolve in this order:

```text
runtime defaults < system settings < project settings < job settings
```

Only registered runtime fields are accepted. Unknown fields are rejected instead of being silently ignored.

## Settings UI

The Web application exposes these routes:

```text
/settings/general
/settings/ai
/settings/agents
/settings/security
/settings/validation
/projects/<project-id>/settings
```

System and project changes create append-only revisions. Updates use optimistic locking, and rollback creates a new revision rather than deleting history.

Startup-only fields are read-only in the UI and require a service restart after changing the JSON/YAML file, environment, or CLI arguments.

## Secrets

Configuration files and settings APIs store secret references, not secret values. Provider keys continue to come from the Worker environment or the deployment secret manager:

```text
AI_JSUNPACK_AGENT_API_KEY
AI_JSUNPACK_LOCAL_AGENT_API_KEY
AI_JSUNPACK_BROWSER_RUNNER_TOKEN
AI_JSUNPACK_AUTH_SECRET
```

The effective-config and readiness APIs return only sanitized configuration and boolean credential status. They never return the secret value.

## Production Profile

Set `shared.deploymentProfile` to `production` for deployed environments. In production:

- local host sandbox execution is denied;
- local Playwright fallback is denied;
- cloud OpenAI-compatible endpoints require the configured endpoint security policy;
- missing isolation must fail closed instead of silently using a local runner.

Use the committed example only as a development starting point. A production file should select an isolated sandbox and a remote Browser Runner.
