# What-if scenarios runner — JNPA UC-III PoC (Sub-Criterion 5).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Shared lib (with otel extra for cross-service tracing), then the service.
COPY shared/ /app/shared/
RUN pip install --upgrade pip && pip install "/app/shared[otel]"

COPY scenarios/pyproject.toml /app/scenarios/pyproject.toml
COPY scenarios/README.md /app/scenarios/README.md
COPY scenarios/ /app/scenarios/

RUN pip install -e /app/scenarios

# Import guard.
RUN python -c "import scenarios, scenarios.runner; print('scenarios import OK')"

EXPOSE 8400

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD curl -fsS http://localhost:8400/healthz >/dev/null || exit 1

CMD ["scenarios-runner"]
