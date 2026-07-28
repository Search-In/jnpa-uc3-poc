# EIR OCR ingest — JNPA UC-III PoC.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Etc/UTC \
    PYTHONPATH=/app/ingest

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        tesseract-ocr \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY ingest/eir_ocr/pyproject.toml /app/ingest/eir_ocr/pyproject.toml
COPY ingest/eir_ocr/README.md /app/ingest/eir_ocr/README.md
COPY ingest/eir_ocr/ /app/ingest/eir_ocr/

RUN pip install --upgrade pip && pip install -e /app/ingest/eir_ocr

RUN python -c "import eir_ocr.app, eir_ocr.engine, eir_ocr.extract; print('eir_ocr import OK')"

EXPOSE 8210

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8210/healthz >/dev/null || exit 1

CMD ["eir-ocr"]
