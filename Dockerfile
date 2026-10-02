# Stream Graph web server (FastAPI + spider scheduler + web/ frontend).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
# traceroute: the escalation diagnostics (UDP mode, works without root).
RUN apt-get update && apt-get install -y --no-install-recommends traceroute && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model (MiniLM, ONNX) into the image, so it works without internet access at runtime.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed HF_HUB_DISABLE_TELEMETRY=1
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2', cache_dir='/opt/fastembed')" \
 && chmod -R a+rX /opt/fastembed

COPY *.py ./
COPY routes ./routes
COPY web ./web
COPY evals ./evals

# Run as an unprivileged user; its state (scheduler on/off + interval) lives in a volume.
RUN useradd --create-home --uid 10001 app && mkdir -p /app/state /app/backups && chown app /app/state /app/backups
USER app
ENV SCHEDULER_FILE=/app/state/scheduler.json \
    EMBEDDINGS_CRON_FILE=/app/state/embeddings_cron.json \
    METRICS_DB=/app/state/metrics.db \
    BACKUP_DIR=/app/backups \
    NOTIFICATIONS_FILE=/app/state/notifications.json \
    MPLCONFIGDIR=/home/app/.cache/matplotlib

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
