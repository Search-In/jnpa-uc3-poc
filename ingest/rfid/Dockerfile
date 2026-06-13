# RFID emulator / consumer / correlator — JNPA UC-III PoC.
# One image, three console scripts (the compose service picks which via command:).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared library first (its own pinned deps), then the service.
# Build context is the repo root (see docker-compose build.context).
COPY shared/ /app/shared/
RUN pip install --upgrade pip && pip install /app/shared

COPY ingest/rfid/pyproject.toml /app/ingest/rfid/pyproject.toml
COPY ingest/rfid/README.md /app/ingest/rfid/README.md
COPY ingest/rfid/ /app/ingest/rfid/

# Editable install so the running code is exactly the COPY'd source.
RUN pip install -e /app/ingest/rfid

# Import guard: fail the build loudly if anything is broken.
RUN python -c "import emulator, consumer, correlator; import rfid_ingest.topology; print('rfid_ingest import OK')"

EXPOSE 9102

# Metrics liveness — if /metrics answers, the service loop is up.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:9102/metrics >/dev/null || exit 1

# Default to the emulator; compose overrides command: for consumer/correlator.
CMD ["rfid-emulator"]
