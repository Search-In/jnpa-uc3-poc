# API gateway + fallback orchestrator — JNPA UC-III PoC (Sub-Criterion 3).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC \
    # Repo root on the path so the package imports as `gateway`.
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared library first (its own pinned deps), then the service.
# Build context is the repo root (see docker-compose build.context).
COPY shared/ /app/shared/
RUN pip install --upgrade pip && pip install /app/shared

COPY gateway/pyproject.toml /app/gateway/pyproject.toml
COPY gateway/README.md /app/gateway/README.md
COPY gateway/ /app/gateway/

RUN pip install -e /app/gateway

# Import guard: fail the build loudly if anything is broken.
RUN python -c "import gateway.main, gateway.fallback, gateway.cache; print('gateway import OK')"

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD curl -fsS http://localhost:8000/healthz >/dev/null || exit 1

CMD ["jnpa-gateway"]
