#!/bin/bash
# ============================================================
# WCS Health Check — protocol-aware (HTTP or HTTPS)
# ============================================================
API_PORT="${API_PORT:-8000}"

if [ -f /tmp/ssl-certs/fullchain.pem ] && [ -f /tmp/ssl-certs/privkey.pem ]; then
    # HTTPS mode — use -k (insecure) because localhost != cert domain
    exec curl -skf "https://localhost:${API_PORT}/api/v1/health"
else
    # HTTP mode
    exec curl -sf "http://localhost:${API_PORT}/api/v1/health"
fi
