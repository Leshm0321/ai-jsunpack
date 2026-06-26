FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY apps/api ./apps/api
COPY apps/worker ./apps/worker
COPY apps/browser_runner ./apps/browser_runner
COPY packages ./packages

EXPOSE 8000

CMD ["uvicorn", "apps.browser_runner.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
