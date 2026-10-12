# Stream Graph web server (FastAPI + spider scheduler + web/ frontend).
# Pinned to an exact base image: the plain tag is republished often, and every new one throws away the build cache
# (a full ~10 min rebuild). To take a newer base (security fixes), update the digest:
#   docker buildx imagetools inspect python:3.12-slim   (the "Digest:" line)
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
# traceroute: the escalation diagnostics (UDP mode, works without root).
RUN apt-get update && apt-get install -y --no-install-recommends traceroute && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model (MiniLM) and the chatbot's reranker (ms-marco MiniLM cross-encoder), both ONNX, into the image, so it works without internet access at runtime.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed HF_HUB_DISABLE_TELEMETRY=1
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2', cache_dir='/opt/fastembed')" \
 && python -c "from fastembed.rerank.cross_encoder import TextCrossEncoder; TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2', cache_dir='/opt/fastembed')" \
 && chmod -R a+rX /opt/fastembed

# No local neural voice in the image any more: alerts are spoken by the browser's own Indian voice (web/tts-alerts.js).
# voice_neural.py reports itself unavailable without Piper, and the server voice then hands the text to the browser.

# Run as an unprivileged user; its state (scheduler on/off + interval) lives in a volume.
RUN useradd --create-home --uid 10001 app && mkdir -p /app/state /app/backups && chown app /app/state /app/backups

# The code last: a code-only change rebuilds just these layers.
COPY *.py ./
COPY routes ./routes
COPY web ./web
COPY evals ./evals
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
