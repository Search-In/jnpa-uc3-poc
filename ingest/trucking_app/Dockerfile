# Trucking-app GPS telemetry simulator — JNPA UC-III PoC (Sub-Criterion 1D).
# FastAPI control plane on 8240 + asyncio/uvloop fleet engine in one image.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC \
    # The package imports as `trucking_app` and the entrypoint as `app`.
    PYTHONPATH=/app/ingest/trucking_app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared library first (its own pinned deps), then the service.
# Build context is the repo root (see docker-compose build.context).
COPY shared/ /app/shared/
RUN pip install --upgrade pip && pip install "/app/shared[otel]"

COPY ingest/trucking_app/pyproject.toml /app/ingest/trucking_app/pyproject.toml
COPY ingest/trucking_app/README.md /app/ingest/trucking_app/README.md
COPY ingest/trucking_app/ /app/ingest/trucking_app/

# Editable install so the running code is exactly the COPY'd source.
RUN pip install -e /app/ingest/trucking_app

# Import guard: fail the build loudly if anything is broken.
RUN python -c "import app; import trucking_app.fleet, trucking_app.simulator, trucking_app.routing; print('trucking_app import OK')"

EXPOSE 8240

# Liveness — if /healthz answers, the control plane (and its fleet) is up.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -fsS http://localhost:8240/healthz >/dev/null || exit 1

CMD ["truck-sim"]
