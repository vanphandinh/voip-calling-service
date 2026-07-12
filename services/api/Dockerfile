# ============================================================
# Calling API Service — Dockerfile
# ============================================================
# Runtime dependencies:
#   - ffmpeg        (TTS audio conversion + RTP streaming)
#   - espeak-ng     (offline TTS fallback)
#   - Python 3.11+  (API server + SIP signaling via raw sockets)
# ============================================================

FROM python:3.11-slim-bookworm

LABEL org.opencontainers.image.title="WCS Calling API"
LABEL org.opencontainers.image.description="VoIP Announcement Calling Service"
LABEL org.opencontainers.image.version="1.0.0"

ENV DEBIAN_FRONTEND=noninteractive

# ------------------------------------------------------------------
# System dependencies
# ------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    # TTS fallback
    espeak-ng \
    espeak-ng-data \
    # TTS audio conversion + RTP streaming
    ffmpeg \
    # Utilities
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------------
# Python dependencies
# ------------------------------------------------------------------
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ------------------------------------------------------------------
# Application code
# ------------------------------------------------------------------
COPY app/ ./app/

# Create directories and non-root user
RUN mkdir -p /audio && \
    useradd --create-home --shell /bin/bash wcs && \
    chown -R wcs:wcs /app /audio

USER wcs
WORKDIR /app

# ------------------------------------------------------------------
# Health check
# ------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:8000/api/v1/health || exit 1

# ------------------------------------------------------------------
# Run
# ------------------------------------------------------------------
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
