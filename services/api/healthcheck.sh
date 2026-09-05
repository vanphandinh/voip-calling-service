#!/bin/bash
# ============================================================
# WCS Health Check — protocol-aware (HTTP or HTTPS)
# ============================================================
# NOTE: the API always listens on port 8000 INSIDE the container.
# API_PORT in docker-compose only remaps the HOST port
# ("${API_PORT}:8000"), so the health check must use 8000.
CONTAINER_PORT=8000

if [ -f /tmp/ssl-certs/fullchain.pem ] && [ -f /tmp/ssl-certs/privkey.pem ]; then
    # HTTPS mode — use -k (insecure) because localhost != cert domain
    exec curl -skf "https://localhost:${CONTAINER_PORT}/api/v1/health"
else
    # HTTP mode
    exec curl -sf "http://localhost:${CONTAINER_PORT}/api/v1/health"
fi
