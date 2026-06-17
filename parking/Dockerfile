# Parking-availability service — JNPA UC-III PoC.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC \
    # repo root is on the path so the package imports as `parking`.
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared library first (its own pinned deps), then the service.
# Build context is the repo root (see docker-compose build.context).
COPY shared/ /app/shared/
RUN pip install --upgrade pip && pip install /app/shared

COPY parking/pyproject.toml /app/parking/pyproject.toml
COPY parking/README.md /app/parking/README.md
COPY parking/ /app/parking/

RUN pip install -e /app/parking

# Import guard: fail the build loudly if anything is broken.
RUN python -c "import parking.app, parking.facilities; print('parking import OK')"

EXPOSE 8370

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8370/healthz >/dev/null || exit 1

CMD ["parking"]
