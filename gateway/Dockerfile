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
RUN pip install --upgrade pip && pip install "/app/shared[otel]"

COPY gateway/pyproject.toml /app/gateway/pyproject.toml
COPY gateway/README.md /app/gateway/README.md
COPY gateway/ /app/gateway/

RUN pip install -e /app/gateway

# Appendix-C capability packages. The gateway proxies these services but also
# imports their deterministic logic in-process as the SYNTHETIC fallback (so the
# dashboard renders Empty-Container / Carbon / Auto-LEO / Identity / Parking even
# when a service is down). Installing them editable makes the hyphenated dirs
# importable as empty_container / carbon / gate_data / identity / parking.
COPY empty-container/ /app/empty-container/
COPY carbon/ /app/carbon/
COPY gate-data/ /app/gate-data/
COPY identity/ /app/identity/
COPY parking/ /app/parking/
RUN pip install -e /app/empty-container -e /app/carbon -e /app/gate-data \
        -e /app/identity -e /app/parking

# Guarantee starlette is intact: the original build landed a half-written
# starlette (Version: None -> `import starlette.exceptions` fails) which only
# surfaced at runtime, not at the build import-guard. Force a clean reinstall of
# the fastapi/starlette pair so the ASGI stack is whole.
RUN pip install --force-reinstall --no-deps "fastapi==0.115.*" \
    && pip install --force-reinstall "starlette>=0.37,<0.42"

# Chromium for server-side PDF rendering (/api/reports/police?format=pdf).
# `--with-deps` is avoided: on Debian-slim/arm64 it tries to apt-install Ubuntu
# font packages that don't exist there and aborts. Instead we apt-install the
# Chromium runtime libs that DO exist on Debian, then fetch the browser binary.
# The whole block is best-effort (|| true): if Chromium can't be provisioned the
# police-report route degrades to printable HTML rather than failing the build.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
        fonts-liberation fonts-freefont-ttf \
    && rm -rf /var/lib/apt/lists/* || true
RUN python -m playwright install chromium || true

# Import guard: fail the build loudly if anything is broken. Import the ASGI
# stack explicitly so a half-written starlette is caught at build, not runtime.
RUN python -c "import starlette.exceptions, fastapi; from starlette.exceptions import HTTPException; print('asgi stack OK', fastapi.__version__)" \
    && python -c "import gateway.main, gateway.fallback, gateway.cache; print('gateway import OK')" \
    && python -c "import empty_container.optimizer, carbon.calculator, gate_data.leo, identity.embeddings, parking.facilities; print('capability fallbacks OK')"

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD curl -fsS http://localhost:8000/healthz >/dev/null || exit 1

CMD ["jnpa-gateway"]
