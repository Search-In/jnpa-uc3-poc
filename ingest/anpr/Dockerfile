# ANPR ingestion service — JNPA UC-III PoC.
# CPU-only; ultralytics pulls a CPU torch wheel. No GPU required for the PoC.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Allow pip to install into the system interpreter (PEP 668). The spec
    # permits --break-system-packages inside containers; this is the env form.
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC \
    # ultralytics writes its config/settings here; use a writable path.
    YOLO_CONFIG_DIR=/tmp/ultralytics \
    MPLCONFIGDIR=/tmp/mpl

# System deps: ffmpeg for video decode, plus the few shared libs OpenCV needs
# even in headless mode.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libglib2.0-0 \
        libgl1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared library first (its own pinned deps), then the service.
# Build context is the repo root (see docker-compose build.context).
COPY shared/ /app/shared/
RUN pip install --upgrade pip && pip install /app/shared

# Install CPU-only torch/torchvision first from the CPU wheel index so
# ultralytics does not pull multi-GB CUDA builds (the PoC is CPU-only).
# --ignore-installed sympy works around the slim base image shipping a
# distutils-installed sympy that pip cannot uninstall when torch upgrades it.
RUN pip install --ignore-installed sympy \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch>=1.8.0" "torchvision>=0.9.0"

# Cache-bust the source layers: pass --build-arg CACHEBUST=$(date +%s) to force
# a fresh COPY of the service source even when Docker would otherwise reuse a
# cached layer.
ARG CACHEBUST=0
COPY ingest/anpr/pyproject.toml /app/ingest/anpr/pyproject.toml
COPY ingest/anpr/README.md /app/ingest/anpr/README.md
COPY ingest/anpr/src/ /app/ingest/anpr/src/

# Install the service deps (torch already satisfied from the CPU index above).
# Editable install so the running code is exactly the COPY'd source — no
# site-packages copy to drift out of sync.
RUN pip install -e /app/ingest/anpr

# ultralytics declares a dep on the GUI OpenCV build, which shadows the headless
# one and pulls libGL. Drop it (ignore "not installed") and pin headless.
RUN pip uninstall -y opencv-python 2>/dev/null; \
    pip install --no-deps "opencv-python-headless==4.10.*"

# Build-time ABI guard: cv2 + torch + numpy must import together, and the
# service package must import. Fails the build loudly if any is broken.
RUN python -c "import cv2, torch, numpy; print('cv2', cv2.__version__, '| torch', torch.__version__, '| numpy', numpy.__version__)" \
    && python -c "import anpr_ingest.main, anpr_ingest.config, anpr_ingest.metrics; print('anpr_ingest import OK')"

# Clips are bind-mounted here by compose; create it so a zero-clip start works.
RUN mkdir -p /data/clips

EXPOSE 9101

# Metrics liveness — if /metrics answers, the asyncio loop is up.
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD curl -fsS http://localhost:9101/metrics >/dev/null || exit 1

CMD ["anpr-ingest"]
