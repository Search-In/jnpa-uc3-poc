# Live Surepass-backed Vahan/Sarathi/FASTag adapter — JNPA UC-III PoC.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC \
    PYTHONPATH=/app/ingest

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY shared/ /app/shared/
RUN pip install --upgrade pip && pip install /app/shared

COPY ingest/vahan_live/pyproject.toml /app/ingest/vahan_live/pyproject.toml
COPY ingest/vahan_live/README.md /app/ingest/vahan_live/README.md
COPY ingest/vahan_live/ /app/ingest/vahan_live/

RUN pip install -e /app/ingest/vahan_live

RUN python -c "import vahan_live.app, vahan_live.mappers; print('vahan_live import OK')"

EXPOSE 8202

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8202/healthz >/dev/null || exit 1

CMD ["vahan-live"]
