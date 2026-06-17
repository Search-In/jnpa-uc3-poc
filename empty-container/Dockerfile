# Empty-container supply-demand optimiser — JNPA UC-III PoC.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC \
    # empty-container/ is on the path so the package imports as `empty_container`.
    PYTHONPATH=/app/empty-container

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared library first (its own pinned deps), then the service.
# Build context is the repo root (see docker-compose build.context).
COPY shared/ /app/shared/
RUN pip install --upgrade pip && pip install /app/shared

COPY empty-container/pyproject.toml /app/empty-container/pyproject.toml
COPY empty-container/README.md /app/empty-container/README.md
COPY empty-container/ /app/empty-container/

RUN pip install -e /app/empty-container

# Import guard: fail the build loudly if anything is broken.
RUN python -c "import empty_container.app, empty_container.optimizer, empty_container.seed; print('empty_container import OK')"

EXPOSE 8330

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8330/healthz >/dev/null || exit 1

CMD ["empty-container"]
