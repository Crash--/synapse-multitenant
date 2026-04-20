#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

HOST="${1:-localhost}"
PORT="${2:-443}"
SCHEME="${SCHEME:-https}"
DEFAULT_PORT=443
[ "$SCHEME" = "http" ] && DEFAULT_PORT=80
BASE_URL="${SCHEME}://${HOST}"
if [ "$PORT" != "$DEFAULT_PORT" ]; then
  BASE_URL="${SCHEME}://${HOST}:${PORT}"
fi

echo "============================================"
echo "  k6 Multi-Tenant Stress Test"
echo "  Target: ${BASE_URL}"
echo "============================================"

# ── Pre-flight: check Synapse is running ──────────────────────
echo ""
echo "Checking Synapse health..."
if ! curl -skf "${BASE_URL}/health" -H "Host: stress-a.localhost" > /dev/null 2>&1; then
  echo "ERROR: Synapse is not responding at ${BASE_URL}"
  echo "Make sure the docker-compose stack is running:"
  echo "  cd docker-demo && docker compose up -d"
  exit 1
fi
echo "Synapse is healthy."

# ── Start observability stack (InfluxDB + Grafana) ────────────
echo ""
echo "=== Starting InfluxDB + Grafana ==="
docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d 2>&1

# Wait for InfluxDB to be ready
echo "Waiting for InfluxDB..."
for i in $(seq 1 20); do
  if curl -sf http://localhost:8086/ping > /dev/null 2>&1; then
    echo "InfluxDB is ready."
    break
  fi
  sleep 1
done

echo ""
echo "  Grafana dashboard: http://localhost:3030/d/k6-multitenant"
echo "  InfluxDB:          http://localhost:8086"
echo ""

# ── Phase 1: Bootstrap users and rooms ────────────────────────
echo "=== Phase 1: Bootstrap ==="
python3 setup.py --host "$HOST" --port "$PORT" --scheme "$SCHEME"

# ── Phase 2: Run k6 with InfluxDB output ─────────────────────
echo ""
echo "=== Phase 2: k6 Load Test ==="
echo "  Open Grafana to watch live: http://localhost:3030/d/k6-multitenant"
echo ""

INFLUX_OUT="influxdb=http://localhost:8086/k6"

K6_SUMMARY="${SCRIPT_DIR}/k6-summary.json"
if command -v k6 > /dev/null 2>&1; then
  k6 run \
    --out "$INFLUX_OUT" \
    --env BASE_URL="${BASE_URL}" \
    --summary-export="$K6_SUMMARY" \
    stress-test.js
else
  echo "k6 not found locally, using Docker..."
  docker run --rm \
    --network docker-demo_synapse-demo-net \
    -v "${SCRIPT_DIR}:/scripts" \
    -w /scripts \
    grafana/k6 run \
    --out "influxdb=http://stress-influxdb:8086/k6" \
    --env BASE_URL="https://synapse-demo-traefik" \
    --summary-export=/scripts/k6-summary.json \
    stress-test.js
fi

echo ""
echo "=== Done ==="
echo ""
echo "Results are in Grafana: http://localhost:3030/d/k6-multitenant"
echo "To tear down observability stack:"
echo "  docker compose -f $SCRIPT_DIR/docker-compose.yml down -v"
