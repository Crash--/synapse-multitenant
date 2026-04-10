#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

HOST="${1:-localhost}"
PORT="${2:-80}"
BASE_URL="http://${HOST}"
if [ "$PORT" != "80" ]; then
  BASE_URL="http://${HOST}:${PORT}"
fi

echo "============================================"
echo "  k6 Multi-Tenant Stress Test"
echo "  Target: ${BASE_URL}"
echo "============================================"

# ── Pre-flight: check Synapse is running ──────────────────────
echo ""
echo "Checking Synapse health..."
if ! curl -sf "${BASE_URL}/health" -H "Host: matrix.tenant-a.com" > /dev/null 2>&1; then
  echo "ERROR: Synapse is not responding at ${BASE_URL}"
  echo "Make sure the docker-compose stack is running:"
  echo "  cd docker-demo && docker compose up -d"
  exit 1
fi
echo "Synapse is healthy."

# ── Phase 1: Bootstrap users and rooms ────────────────────────
echo ""
echo "=== Phase 1: Bootstrap ==="
python3 setup.py --host "$HOST" --port "$PORT"

# ── Phase 2: Run k6 ──────────────────────────────────────────
echo ""
echo "=== Phase 2: k6 Load Test ==="

if command -v k6 > /dev/null 2>&1; then
  k6 run --env BASE_URL="${BASE_URL}" stress-test.js
else
  echo "k6 not found locally, using Docker..."
  docker run --rm \
    --network docker-demo_synapse-demo-net \
    -v "${SCRIPT_DIR}:/scripts:ro" \
    -w /scripts \
    grafana/k6 run \
    --env BASE_URL="http://traefik" \
    stress-test.js
fi

echo ""
echo "=== Done ==="
