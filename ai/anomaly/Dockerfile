# Behavioural anomaly detector (ByteTrack + rules + trajectory AE) — JNPA UC-III.
# CPU-only image. torch + the ByteTrack stack (supervision/ultralytics) are
# installed from the PyTorch CPU wheel index so we do not pull multi-GB CUDA
# builds. The rule engine + telemetry path run without them; ByteTrack activates
# only when the `track` extra is present (it is, in this image). Serves the
# FastAPI app on 8321.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC \
    # The flat-layout deliverables import as the `anomaly` package via this.
    PYTHONPATH=/app/ai

# curl for the healthcheck; OpenCV needs libGL/glib at runtime even for the
# headless wheel's video decode path; build tools occasionally needed by wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Shared library first (its own pinned deps). Build context is the repo root
# (see docker-compose build.context).
COPY shared/ /app/shared/
RUN pip install --upgrade pip && pip install "/app/shared[otel]"

# CPU-only torch from the PyTorch wheel index (keeps the image lean). ultralytics
# + supervision then resolve against this torch instead of pulling a CUDA build.
RUN pip install --ignore-installed sympy \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.0"

ARG CACHEBUST=0
COPY ai/anomaly/pyproject.toml /app/ai/anomaly/pyproject.toml
COPY ai/anomaly/README.md /app/ai/anomaly/README.md
COPY ai/anomaly/__init__.py /app/ai/anomaly/__init__.py
COPY ai/anomaly/*.py /app/ai/anomaly/
COPY ai/anomaly/rules/ /app/ai/anomaly/rules/
COPY ai/anomaly/autoencoder/ /app/ai/anomaly/autoencoder/
COPY ai/anomaly/track/ /app/ai/anomaly/track/

# Install the service deps + the ByteTrack stack (supervision/ultralytics). The
# package itself is also importable via PYTHONPATH; the editable install wires
# the console scripts + pulls fastapi/kafka/redis/minio/psycopg.
RUN pip install -e "/app/ai/anomaly[track]"

# Pre-fetch the YOLOv8n weights so the first frame doesn't block on a download
# (best-effort; the tracker degrades gracefully if this is skipped).
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')" || true

# Build-time import guard.
RUN python -c "import anomaly.engine, anomaly.rules.wrongway, anomaly.rules.parking, anomaly.rules.abandoned, anomaly.rules.route_deviation; print('rules import OK')" \
    && python -c "import anomaly.autoencoder.model, anomaly.autoencoder.features; print('ae import OK')" \
    && python -c "import anomaly.track.bytetrack as bt; print('bytetrack import OK, deps=', bt.deps_available())" \
    && python -c "import anomaly.app; print('app import OK')"

EXPOSE 8321

# Readiness: /health answers once the engine + workers are constructed.
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=8 \
    CMD curl -fsS http://localhost:8321/health >/dev/null || exit 1

# Entry: serve the API. The AE trains on demand via POST /train_ae (and the
# compose command can warm it on first boot — see docker-compose.yml).
CMD ["anomaly-api"]
