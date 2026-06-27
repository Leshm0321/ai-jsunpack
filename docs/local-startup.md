# Local Startup And Verification

This guide describes the local development flow for API, Web, Worker, and the optional Browser Runner service.

## Environment Files

The repository now includes:

- `.env`: local development defaults. It is ignored by git and contains only local placeholder values.
- `.example.env`: committed template for new local environments.

Do not put `AI_JSUNPACK_SERVICE_ROLE`, `AI_JSUNPACK_SANDBOX_*`, `AI_JSUNPACK_AGENT_*`, or `AI_JSUNPACK_BROWSER_RUNNER_*` in the root `.env`. The API rejects Worker or Browser Runner variables when it starts with `AI_JSUNPACK_SERVICE_ROLE=api`, so each service terminal should set service-specific variables immediately before startup.

Load `.env` in PowerShell:

```powershell
Get-Content .env | ForEach-Object {
  if ($_ -and -not $_.TrimStart().StartsWith("#")) {
    $name, $value = $_.Split("=", 2)
    Set-Item -Path "Env:$name" -Value $value
  }
}
```

## Install Dependencies

```powershell
npm install
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install
```

For static checks:

```powershell
.venv\Scripts\python.exe -m pip install -e .[dev]
```

## Generate Local Tokens

The API uses HMAC Bearer tokens signed with `AI_JSUNPACK_AUTH_SECRET`.

User token for Web/API:

```powershell
$env:VITE_API_AUTH_TOKEN = .venv\Scripts\python.exe -c "from apps.api.app.auth import create_auth_token; print(create_auth_token(subject='local-user', projects={'default':'owner'}, secret='dev-secret', ttl_seconds=86400))"
```

Worker service token for Browser Runner:

```powershell
$env:AI_JSUNPACK_BROWSER_RUNNER_TOKEN = .venv\Scripts\python.exe -c "from apps.api.app.auth import create_auth_token; print(create_auth_token(subject='worker-local', kind='service', projects={'default':'owner'}, service_roles=['worker'], secret='dev-secret', ttl_seconds=86400))"
```

## Start Services

Start each service in its own terminal after loading `.env`.

API:

```powershell
$env:AI_JSUNPACK_SERVICE_ROLE = "api"
.venv\Scripts\python.exe -m uvicorn apps.api.app.main:app --reload --host 127.0.0.1 --port 8000
```

Web:

```powershell
npm run dev:web
```

Worker:

```powershell
$env:AI_JSUNPACK_SERVICE_ROLE = "worker"
$env:AI_JSUNPACK_WORKER_ID = "worker-local-1"
$env:AI_JSUNPACK_WORKER_LEASE_SECONDS = "300"
$env:AI_JSUNPACK_WORKER_POLL_SECONDS = "5"
$env:AI_JSUNPACK_WORKER_MAX_ATTEMPTS = "3"
$env:AI_JSUNPACK_SANDBOX_RUNNER = "local"
$env:AI_JSUNPACK_CREWAI_DATA_ROOT = ".crewai-data"
.venv\Scripts\python.exe -m apps.worker.worker.queue
```

Optional Browser Runner:

```powershell
$env:AI_JSUNPACK_SERVICE_ROLE = "browser-runner"
$env:AI_JSUNPACK_BROWSER_RUNNER_QUEUE_BACKEND = "sqlite"
$env:AI_JSUNPACK_BROWSER_RUNNER_WORKDIR = "tmp/local-dev/browser-runner"
$env:AI_JSUNPACK_BROWSER_RUNNER_DB_PATH = "tmp/local-dev/browser-runner/browser-runs.sqlite3"
$env:AI_JSUNPACK_BROWSER_RUNNER_WORKERS = "2"
$env:AI_JSUNPACK_BROWSER_RUNNER_MAX_ATTEMPTS = "3"
$env:AI_JSUNPACK_BROWSER_RUNNER_LEASE_SECONDS = "120"
$env:AI_JSUNPACK_BROWSER_RUNNER_RETRY_BACKOFF_SECONDS = "1"
$env:AI_JSUNPACK_BROWSER_RUNNER_MAX_QUEUE_AGE_MS = "60000"
$env:AI_JSUNPACK_BROWSER_RUNNER_MAX_CLAIM_LATENCY_MS = "60000"
$env:AI_JSUNPACK_BROWSER_RUNNER_MAX_EXPIRED_RUNNING = "0"
$env:AI_JSUNPACK_BROWSER_RUNNER_MAX_RETRY_RATE = "0.25"
.venv\Scripts\python.exe -m uvicorn apps.browser_runner.app.main:app --host 127.0.0.1 --port 8001
```

To make Worker use Browser Runner, set these variables in the Worker terminal before starting Worker:

```powershell
$env:AI_JSUNPACK_BROWSER_RUNNER_URL = "http://127.0.0.1:8001"
$env:AI_JSUNPACK_BROWSER_RUNNER_TOKEN = "<worker service token>"
$env:AI_JSUNPACK_BROWSER_RUNNER_POLL_SECONDS = "0.25"
$env:AI_JSUNPACK_BROWSER_RUNNER_TIMEOUT_MS = "60000"
```

If Browser Runner is not running, keep `AI_JSUNPACK_BROWSER_RUNNER_URL` unset in the Worker terminal so Worker uses the local Playwright adapter:

```powershell
Remove-Item Env:AI_JSUNPACK_BROWSER_RUNNER_URL -ErrorAction SilentlyContinue
Remove-Item Env:AI_JSUNPACK_BROWSER_RUNNER_TOKEN -ErrorAction SilentlyContinue
```

## Health Checks

API:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Browser Runner:

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
```

Web:

```powershell
Start-Process http://127.0.0.1:5173
```

## Regression Verification

Run these before handing off code changes:

```powershell
npm run check
npm run test:core
.venv\Scripts\python.exe -m unittest discover tests
```

Run static checks:

```powershell
.venv\Scripts\python.exe -m ruff check apps packages tests deploy
.venv\Scripts\python.exe -m bandit -c pyproject.toml -r apps packages deploy -x tests
```

Run compile checks:

```powershell
.venv\Scripts\python.exe -m compileall apps packages tests
```

## End-To-End Smoke

Docker Compose rehearsal:

```powershell
.venv\Scripts\python.exe -m deploy.compose_smoke `
  --output tmp\deployment-compose-smoke\compose-smoke.json `
  --artifact-root tmp\deployment-compose-smoke\artifacts `
  --soak-runs 10
```

The report is acceptable when `status` is `pass`.

Lightweight API/Worker smoke without Docker:

```powershell
.venv\Scripts\python.exe -m apps.api.app.deployment_smoke --output tmp\deployment-smoke.json
```

## Expected Local Artifact Locations

- Metadata DB: `tmp/local-dev/metadata.db`
- Artifact files: `tmp/local-dev/artifacts`
- Browser Runner queue DB: `tmp/local-dev/browser-runner/browser-runs.sqlite3`
- CrewAI local data: `.crewai-data`

## Notes

- `.env` is for local development only.
- `.example.env` is the committed template.
- Production secrets must come from CI or a secret manager.
- The local sandbox runner is development/audit-only and is not production multi-tenant isolation.
