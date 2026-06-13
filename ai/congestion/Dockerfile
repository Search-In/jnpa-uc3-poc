# Traffic congestion forecaster (GraphSAGE + LSTM) — JNPA UC-III PoC.
# CPU-only image. torch is installed from the PyTorch CPU wheel index so we do
# not pull multi-GB CUDA builds. The GraphSAGE conv is implemented from scratch
# (no torch-geometric required). Trains a model at build/boot then serves the
# FastAPI app on 8311.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC \
    # The flat-layout deliverables import as the `congestion` package via this.
    PYTHONPATH=/app/ai

# curl for the healthcheck; build tools occasionally needed by wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Shared library first (its own pinned deps). Build context is the repo root
# (see docker-compose build.context).
COPY shared/ /app/shared/
RUN pip install --upgrade pip && pip install "/app/shared[otel]"

# CPU-only torch from the PyTorch wheel index (keeps the image lean).
RUN pip install --ignore-installed sympy \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.0"

ARG CACHEBUST=0
COPY ai/congestion/pyproject.toml /app/ai/congestion/pyproject.toml
COPY ai/congestion/README.md /app/ai/congestion/README.md
COPY ai/congestion/__init__.py /app/ai/congestion/__init__.py
COPY ai/congestion/*.py /app/ai/congestion/
COPY ai/congestion/sources/ /app/ai/congestion/sources/
# Pre-trained weights + metrics (F1 0.84). Baked in so a fresh `make up` serves
# instantly; the entrypoint seeds the writable volume from here. Optional — the
# service retrains if absent.
COPY ai/congestion/artifacts/ /app/ai/congestion/artifacts/

# Install the service deps (the package itself is also importable via PYTHONPATH;
# editable install wires the console scripts + pulls fastapi/kafka/redis/minio).
RUN pip install -e /app/ai/congestion

# Build-time import guard.
RUN python -c "import congestion.graph, congestion.features, congestion.metrics; print('core import OK')" \
    && python -c "import torch, congestion.model, congestion.train, congestion.infer; print('torch+model import OK')" \
    && python -c "import congestion.sources; print('sources import OK')"

EXPOSE 8311

# Readiness: /healthz answers once the model + graph are constructed.
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=8 \
    CMD curl -fsS http://localhost:8311/healthz >/dev/null || exit 1

# Entry: train (idempotent — skips if weights already present in MinIO/local via
# the entrypoint), then serve. Compose overrides command to control this.
CMD ["sh", "-c", "congestion-train || true; congestion-api"]
