#!/usr/bin/env bash
# Install or reinstall ch_logistics on the current bench.
# Run from the bench root: ./apps/ch_logistics/install_on_bench.sh <site>
set -euo pipefail

SITE="${1:-erpnext.local}"
APP="ch_logistics"

if [[ ! -d "apps/${APP}" ]]; then
    echo "Run this from the bench root (where apps/ exists)." >&2
    exit 1
fi

echo "[1/4] Registering app in bench..."
if ! grep -qx "${APP}" sites/apps.txt 2>/dev/null; then
    echo "${APP}" >> sites/apps.txt
fi

echo "[2/4] Installing Python package..."
./env/bin/pip install -e "apps/${APP}" --quiet

echo "[3/4] Installing on site: ${SITE}"
bench --site "${SITE}" install-app "${APP}" || \
    bench --site "${SITE}" migrate

echo "[4/4] Building assets..."
bench build --app "${APP}"

echo ""
echo "Done. Open:"
echo "  /app/live-fleet-map     (dispatcher)"
echo "  /app/driver-map         (driver)"
echo "  /app/ch-tracking-settings  (set Google Maps API key)"
