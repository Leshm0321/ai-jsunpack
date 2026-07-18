FROM docker:29-cli AS docker-cli

FROM node:20-bookworm-slim AS node-deps

WORKDIR /app
COPY package.json package-lock.json tsconfig.base.json ./
COPY packages/shared/package.json ./packages/shared/package.json
COPY packages/core/package.json ./packages/core/package.json
COPY apps/web/package.json ./apps/web/package.json
RUN npm ci

COPY packages/shared ./packages/shared
COPY packages/core ./packages/core
COPY apps/web ./apps/web
RUN npm run build --workspace @ai-jsunpack/shared \
    && npm run build --workspace @ai-jsunpack/core

FROM node:20-bookworm-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    AI_JSUNPACK_CORE_CLI_PATH=/app/packages/core/dist/cli.js

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

COPY requirements.txt pyproject.toml package.json package-lock.json tsconfig.base.json ./
RUN python3 -m pip install --no-cache-dir --break-system-packages --upgrade pip \
    && python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY --from=node-deps /app/node_modules ./node_modules
COPY --from=node-deps /app/packages/shared ./packages/shared
COPY --from=node-deps /app/packages/core ./packages/core
COPY apps/api ./apps/api
COPY apps/worker ./apps/worker
COPY apps/browser_runner ./apps/browser_runner
COPY packages/audit ./packages/audit
COPY packages/configuration ./packages/configuration
COPY packages/deployment ./packages/deployment
COPY packages/knowledge ./packages/knowledge
COPY packages/memory ./packages/memory
COPY packages/sandbox ./packages/sandbox

CMD ["python3", "-m", "apps.worker.worker.queue"]
