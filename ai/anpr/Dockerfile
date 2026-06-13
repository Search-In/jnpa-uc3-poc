# ANPR + OCR inference service — JNPA UC-III PoC.
# CPU-only image. The bid stack (YOLOv8 + PaddleOCR PP-OCRv4) is installed so the
# real recogniser runs in-container; if a model fails to load at runtime the
# service degrades to a classical detector + template OCR (see src/anpr/*).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Allow pip to install into the system interpreter (PEP 668), container-safe.
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC \
    # ultralytics + matplotlib write config here; use writable paths.
    YOLO_CONFIG_DIR=/tmp/ultralytics \
    MPLCONFIGDIR=/tmp/mpl \
    # PaddleOCR caches downloaded base models here.
    HOME=/root

# System deps: OpenCV runtime libs (headless still needs these), ffmpeg for any
# video decode, curl for the healthcheck, plus the libs paddle needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
        curl \
        wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared library first (its own pinned deps), then the service.
# Build context is the repo root (see docker-compose build.context).
COPY shared/ /app/shared/
RUN pip install --upgrade pip && pip install /app/shared

# CPU-only torch first (ultralytics would otherwise pull multi-GB CUDA builds).
# --ignore-installed sympy works around the slim base shipping a distutils sympy
# that pip cannot uninstall when torch upgrades it.
RUN pip install --ignore-installed sympy \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch>=1.8.0" "torchvision>=0.9.0"

ARG CACHEBUST=0
COPY ai/anpr/pyproject.toml /app/ai/anpr/pyproject.toml
COPY ai/anpr/README.md /app/ai/anpr/README.md
COPY ai/anpr/src/ /app/ai/anpr/src/
COPY ai/anpr/eval/ /app/ai/anpr/eval/
COPY ai/anpr/resources/ /app/ai/anpr/resources/

# Install the service + the heavy OCR stack (paddle/paddleocr/ultralytics).
# Editable install so the running code is exactly the COPY'd source.
RUN pip install -e "/app/ai/anpr[ocr]"

# ultralytics + paddleocr both declare the GUI OpenCV build, which shadows the
# headless one and pulls libGL. Drop it and pin headless.
RUN pip uninstall -y opencv-python opencv-contrib-python 2>/dev/null; \
    pip install --no-deps "opencv-python-headless==4.10.*"

# Build-time guard: the core (always-on) stack must import. The heavy OCR stack
# (paddle) is allowed to be absent at build time on some arch/wheel combos — the
# service degrades gracefully — so we don't hard-fail the build on it.
RUN python -c "import cv2, numpy, fastapi; print('core ok | cv2', cv2.__version__)" \
    && python -c "import anpr.app, anpr.pipeline, anpr.postprocess, anpr.evaluator; print('anpr import OK')" \
    && python -c "import anpr.detect, anpr.ocr; print('detect+ocr import OK')"

EXPOSE 8301

# Readiness: /healthz answers once the pipeline is constructed.
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
    CMD curl -fsS http://localhost:8301/healthz >/dev/null || exit 1

CMD ["anpr-ai"]
