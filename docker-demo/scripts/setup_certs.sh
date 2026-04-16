#!/usr/bin/env bash
# Generate an mkcert certificate covering the demo's hostnames.
#
# NOTE on wildcards: mkcert can produce a cert with SAN "*.localhost",
# but Chrome refuses to accept wildcards at second level when the parent
# is on the PSL / reserved-TLD list (localhost is treated specially).
# Firefox and curl accept "*.localhost" fine, but Chrome returns
# ERR_CERT_COMMON_NAME_INVALID. So we list explicit hostnames AND keep
# the wildcard as a fallback for non-Chrome clients.
#
# To add a new tenant name (e.g. "my-new-tenant.localhost") either:
#   (a) use one of the pre-baked hostnames below, or
#   (b) re-run this script with TENANTS="my-new-tenant other-tenant" to
#       regenerate the cert including the new names.
#
# Idempotent: re-runs are no-ops if the cert already covers every needed
# name and isn't expiring soon.
#
# Runs on the HOST (needs access to the host's mkcert CA trust store).
# Pre-req: mkcert installed on the host.

set -euo pipefail

CERT_DIR="${CERT_DIR:-$(cd "$(dirname "$0")/.." && pwd)/certs}"
CERT_FILE="$CERT_DIR/wildcard.crt"
KEY_FILE="$CERT_DIR/wildcard.key"

# Infrastructure hostnames (always included).
INFRA_HOSTS=(
  "localhost"
  "manager.localhost"
  "control-plane.localhost"
  "traefik.localhost"
  "lemonldap.localhost"
  "oidc-proxy.localhost"
  "ldap.localhost"
)

# Pre-baked tenant hostnames (always included — saves regenerating for
# the most common demo tenants). Override via TENANTS="foo bar baz" to
# add more / change the list.
DEFAULT_TENANTS=(
  "acme.localhost"
  "corp.localhost"
  "startup.localhost"
  "tenant-a.localhost"
  "tenant-b.localhost"
  "tenant-c.localhost"
  "tenant-d.localhost"
  "tenant-e.localhost"
  "tenant-f.localhost"
  "tenant-g.localhost"
  "tenant-h.localhost"
  "tenant-i.localhost"
  "tenant-j.localhost"
  "tenant-z.localhost"
  "smoke.localhost"
)

# Build final hosts list. Users can add to it via TENANTS env var.
HOSTS=("${INFRA_HOSTS[@]}" "${DEFAULT_TENANTS[@]}")
if [[ -n "${TENANTS:-}" ]]; then
  # shellcheck disable=SC2206
  EXTRA=( ${TENANTS} )
  for t in "${EXTRA[@]}"; do
    # Auto-suffix bare names.
    [[ "$t" == *.* ]] || t="$t.localhost"
    HOSTS+=("$t")
  done
fi

# Keep the wildcard for Firefox/curl convenience.
HOSTS+=("*.localhost")

if ! command -v mkcert >/dev/null 2>&1; then
  echo "ERROR: mkcert not installed. Install it on your host first:"
  echo "  macOS:         brew install mkcert"
  echo "  Debian/Ubuntu: sudo apt install mkcert"
  echo "  Other:         https://github.com/FiloSottile/mkcert"
  exit 1
fi

mkdir -p "$CERT_DIR"

# Idempotency: skip regeneration if every required SAN is already present
# and the cert isn't expiring within the next week.
if [[ -f "$CERT_FILE" && -f "$KEY_FILE" ]]; then
  ACTUAL_SANS=$(openssl x509 -in "$CERT_FILE" -noout -ext subjectAltName 2>/dev/null | tail -n +2 | tr -d ' \n' || true)
  all_present=true
  for h in "${HOSTS[@]}"; do
    if [[ "$ACTUAL_SANS" != *"$h"* ]]; then
      all_present=false
      break
    fi
  done
  if [[ "$all_present" == true ]] \
     && openssl x509 -in "$CERT_FILE" -noout -checkend 604800 >/dev/null 2>&1; then
    echo "[certs] Existing cert already covers all required hostnames — leaving it."
    echo "[certs] ($CERT_FILE, valid for ≥1 week)"
    exit 0
  fi
fi

echo "[certs] Generating cert for ${#HOSTS[@]} hostnames via mkcert..."
mkcert -install >/dev/null 2>&1 || true
mkcert -cert-file "$CERT_FILE" -key-file "$KEY_FILE" "${HOSTS[@]}"
echo "[certs] Wrote $CERT_FILE and $KEY_FILE"
echo "[certs] Hostnames covered:"
printf '          - %s\n' "${HOSTS[@]}"
