# Gate-data / Auto-LEO simulator — JNPA UC-III PoC.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC \
    # the repo root is on the path so the package imports as `gate_data`.
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared library first (its own pinned deps), then the service.
# Build context is the repo root (see docker-compose build.context).
COPY shared/ /app/shared/
RUN pip install --upgrade pip && pip install /app/shared

COPY gate-data/pyproject.toml /app/gate-data/pyproject.toml
COPY gate-data/README.md /app/gate-data/README.md
COPY gate-data/ /app/gate-data/

RUN pip install -e /app/gate-data

# Import guard: fail the build loudly if anything is broken.
RUN python -c "import gate_data.app, gate_data.leo, gate_data.seed, gate_data.icegate_sim; print('gate_data import OK')"

EXPOSE 8350

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8350/healthz >/dev/null || exit 1

CMD ["gate-data"]
