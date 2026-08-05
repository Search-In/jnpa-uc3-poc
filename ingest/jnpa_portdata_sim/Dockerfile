FROM python:3.13-slim

WORKDIR /app

COPY ingest/jnpa_portdata_sim /app/jnpa_portdata_sim
COPY shared /app/shared

RUN pip install --no-cache-dir fastapi uvicorn && \
    pip install --no-cache-dir -e /app/shared

ENV PYTHONPATH=/app
ENV PORT=8500

EXPOSE 8500

CMD ["python", "-m", "jnpa_portdata_sim.app"]
