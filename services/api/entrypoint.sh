#!/bin/bash
# ============================================================
# WCS Entrypoint — SSL cert setup + privilege drop
# ============================================================
# Runs as ROOT. Copies Let's Encrypt certs to a wcs-readable
# tmpfs location, then drops to the 'wcs' user via gosu.
#
# Fallback: if certs are not available, runs HTTP as before.
# ============================================================
set -euo pipefail

API_PORT="${API_PORT:-8000}"

# --- SSL detection --------------------------------------------------
# SSL_ENABLED: force on/off. Empty = auto-detect.
# SSL_DOMAIN:   your Let's Encrypt domain (e.g. api.example.com)
# -------------------------------------------------------------------

LE_LIVE="/etc/letsencrypt/live"
SSL_CERT_DIR="${SSL_DOMAIN:+${LE_LIVE}/${SSL_DOMAIN}}"
SSL_CERT_FILE="${SSL_CERT_FILE:-${SSL_CERT_DIR:+${SSL_CERT_DIR}/fullchain.pem}}"
SSL_KEY_FILE="${SSL_KEY_FILE:-${SSL_CERT_DIR:+${SSL_CERT_DIR}/privkey.pem}}"

FORCE_DISABLE=$( [ "${SSL_ENABLED:-}" = "false" ] || [ "${SSL_ENABLED:-}" = "0" ] && echo 1 || echo 0 )

if [ "$FORCE_DISABLE" = "1" ]; then
    echo "WCS: SSL disabled via SSL_ENABLED=false — running HTTP on port ${API_PORT}"
    exec gosu wcs "$@"
fi

if [ -z "${SSL_DOMAIN:-}" ]; then
    echo "WCS: SSL_DOMAIN not set — running HTTP on port ${API_PORT}"
    echo "  (Set SSL_DOMAIN and mount /etc/letsencrypt to enable HTTPS)"
    exec gosu wcs "$@"
fi

if [ ! -f "${SSL_CERT_FILE:-}" ] || [ ! -f "${SSL_KEY_FILE:-}" ]; then
    echo "WCS: SSL certs not found at ${SSL_CERT_DIR} — running HTTP on port ${API_PORT}"
    echo "  Checked: ${SSL_CERT_FILE:-<unset>}"
    echo "  Checked: ${SSL_KEY_FILE:-<unset>}"
    exec gosu wcs "$@"
fi

# --- Cert copy to wcs-readable tmpfs --------------------------------
echo "WCS: SSL certs found at ${SSL_CERT_DIR}"

CERT_DEST="/tmp/ssl-certs"
mkdir -p "$CERT_DEST"

cp "$SSL_CERT_FILE" "$CERT_DEST/fullchain.pem"
cp "$SSL_KEY_FILE"  "$CERT_DEST/privkey.pem"

chown -R wcs:wcs "$CERT_DEST"
chmod 644 "$CERT_DEST/fullchain.pem"
chmod 600 "$CERT_DEST/privkey.pem"

echo "WCS: HTTPS enabled on port ${API_PORT} (certs copied to ${CERT_DEST})"

# --- Launch uvicorn with SSL, as wcs user ---------------------------
exec gosu wcs "$@" \
    --ssl-certfile "$CERT_DEST/fullchain.pem" \
    --ssl-keyfile  "$CERT_DEST/privkey.pem"
