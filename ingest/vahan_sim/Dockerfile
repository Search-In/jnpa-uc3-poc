# Vahan/Sarathi/FASTag simulator — JNPA UC-III PoC.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC \
    # ingest/ is on the path so the package imports as `vahan_sim`.
    PYTHONPATH=/app/ingest

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared library first (its own pinned deps), then the service.
# Build context is the repo root (see docker-compose build.context).
COPY shared/ /app/shared/
RUN pip install --upgrade pip && pip install /app/shared

COPY ingest/vahan_sim/pyproject.toml /app/ingest/vahan_sim/pyproject.toml
COPY ingest/vahan_sim/README.md /app/ingest/vahan_sim/README.md
COPY ingest/vahan_sim/ /app/ingest/vahan_sim/

RUN pip install -e /app/ingest/vahan_sim

# Import guard: fail the build loudly if anything is broken.
RUN python -c "import vahan_sim.app, vahan_sim.seed; print('vahan_sim import OK')"

# Fixtures are written here at startup; create it so a read-only data mount works.
RUN mkdir -p /data/fixtures

EXPOSE 8201

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8201/healthz >/dev/null || exit 1

CMD ["vahan-sim"]
