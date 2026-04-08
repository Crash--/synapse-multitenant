#!/bin/bash
# Phase 1B isolation probes run from the host (needs docker CLI access).
#
# Usage: ./scripts/run_isolation_probes_host.sh
# Run from the docker-multitenant/ directory or from anywhere — the script
# cd's to the docker-multitenant/ root before doing anything.

set -u
cd "$(dirname "$0")/.." || { echo "ERROR: failed to cd to docker-multitenant/ root" >&2; exit 2; }

echo "=== PHASE 1B ISOLATION PROBES (host) ==="
echo

overall_rc=0

# ---- Probe: stream-sequence presence ----
# Under correct isolation, each tenant has its OWN events_stream_seq
# inside its schema. Under the fall-through bug, tenant_acme_localhost.
# events_stream_seq does not exist and psql returns non-zero — that IS
# the red signal. This probe asserts presence+readability per-tenant,
# which is sufficient for the phase-1 gate. A stronger independence
# check (nextval on one, verify the other unchanged) is deferred to
# phase 2 probes.
echo "-- stream-sequence presence --"
for schema in tenant_acme tenant_corp tenant_startup; do
    out=$(docker compose exec -T postgres psql -U synapse -d synapse_multitenant -At \
          -c "SELECT last_value FROM ${schema}.events_stream_seq;" 2>&1)
    rc=$?
    if [ $rc -ne 0 ]; then
        echo "    [FAIL] ${schema}.events_stream_seq missing or unreadable"
        echo "      psql output: ${out}"
        overall_rc=1
        continue
    fi
    echo "    [info] ${schema}.events_stream_seq.last_value = ${out}"
done

if [ $overall_rc -eq 0 ]; then
    echo "    [PASS] stream-sequence presence (each tenant has its own events_stream_seq in its schema)"
fi
echo

# ---- Probe: same-localpart HTTP via the in-container test runner ----
# The test container has urllib access to the nginx/Traefik front door via
# the compose network, which the host does not (by default). Delegate the
# HTTP probe back into the container.
echo "-- same-localpart HTTP (delegated to in-container test runner) --"
docker compose run --rm test python /scripts/test_tenants.py --phase 1b-http
http_rc=$?
if [ $http_rc -ne 0 ]; then
    overall_rc=1
fi
echo

if [ $overall_rc -eq 0 ]; then
    echo "=== PHASE 1B ISOLATION PROBES: all PASS ==="
else
    echo "=== PHASE 1B ISOLATION PROBES: FAILURES above ==="
fi
exit $overall_rc
