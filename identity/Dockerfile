# Identity / face-recognition driver verifier — JNPA UC-III PoC.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC \
    # repo root is on the path so the package imports as `identity`.
    PYTHONPATH=/app

# curl for the healthcheck; libgl1 + libglib2.0-0 are OpenCV runtime libs needed
# by the ONNX/ArcFace face-recognition path (opencv-python-headless).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared library first (its own pinned deps), then the service.
# Build context is the repo root (see docker-compose build.context).
COPY shared/ /app/shared/
RUN pip install --upgrade pip && pip install /app/shared

COPY identity/pyproject.toml /app/identity/pyproject.toml
COPY identity/README.md /app/identity/README.md
COPY identity/ /app/identity/

# Install with the onnx extra so the real ArcFace face-recognition path is
# available (onnxruntime + opencv). Degrades to synthetic if no model is mounted.
RUN pip install -e "/app/identity[onnx]"

# Import guard: fail the build loudly if anything is broken.
RUN python -c "import identity.app, identity.embeddings, identity.gallery; print('identity import OK')"

EXPOSE 8360

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8360/healthz >/dev/null || exit 1

CMD ["identity"]
