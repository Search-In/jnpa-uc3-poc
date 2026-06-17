# Carbon-emissions calculator (C6) — JNPA UC-III PoC.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC \
    # repo root is on the path so the package imports as `carbon`.
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared library first (its own pinned deps), then the service.
# Build context is the repo root (see docker-compose build.context).
COPY shared/ /app/shared/
RUN pip install --upgrade pip && pip install /app/shared

COPY carbon/pyproject.toml /app/carbon/pyproject.toml
COPY carbon/README.md /app/carbon/README.md
COPY carbon/ /app/carbon/

RUN pip install -e /app/carbon

# Import guard: fail the build loudly if anything is broken.
RUN python -c "import carbon.app, carbon.calculator, carbon.factors; print('carbon import OK')"

EXPOSE 8340

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8340/healthz >/dev/null || exit 1

CMD ["carbon"]
