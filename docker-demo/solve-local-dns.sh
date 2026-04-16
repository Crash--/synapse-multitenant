#!/usr/bin/env bash
#
# Free port 53 on the host so a containerised DNS server (e.g. dnsmasq)
# can bind it without colliding with systemd-resolved's stub listener
# on 127.0.0.53.
#
# Approach: write a systemd-resolved drop-in that sets DNSStubListener=no,
# restart systemd-resolved, and point /etc/resolv.conf at the real
# upstream resolvers. The drop-in is the systemd-idiomatic way to
# customise a shipped config — cleaner than patching the file in place.
#
# Idempotent: safe to re-run. Rollback: delete the drop-in and re-symlink
# /etc/resolv.conf to stub-resolv.conf. A rollback helper prints at end.

set -euo pipefail

DROP_IN_DIR="/etc/systemd/resolved.conf.d"
DROP_IN_FILE="${DROP_IN_DIR}/00-disable-stub-listener.conf"
RESOLV_CONF="/etc/resolv.conf"
RESOLV_STUB="/run/systemd/resolve/stub-resolv.conf"
RESOLV_REAL="/run/systemd/resolve/resolv.conf"

log() { printf '\033[1;34m[dns-fix]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[dns-fix]\033[0m %s\n' "$*" >&2; }
err() { printf '\033[1;31m[dns-fix]\033[0m %s\n' "$*" >&2; }

# --- Pre-flight ---------------------------------------------------------------

log "[1/6] Pre-flight checks..."

if ! command -v systemctl >/dev/null 2>&1; then
  err "systemctl not found. This script only handles systemd-resolved setups."
  err "You are probably fine already — if not, check whatever DNS resolver"
  err "is holding port 53 on your host and disable it manually."
  exit 1
fi

if ! systemctl list-unit-files systemd-resolved.service >/dev/null 2>&1; then
  err "systemd-resolved is not installed on this system. Nothing to do."
  err "If port 53 is occupied, something else is holding it. Run:"
  err "    sudo ss -tulnp | grep ':53 '"
  exit 1
fi

# Port 53 occupancy check — we only proceed if systemd-resolved (or nothing)
# is on 127.0.0.53.
if command -v ss >/dev/null 2>&1; then
  port53_users=$(sudo ss -tulnp 2>/dev/null | awk '$5 ~ /:53$/' || true)
  if [[ -n "$port53_users" ]] \
     && ! grep -q 'systemd-resolve' <<<"$port53_users" \
     && ! grep -q '127\.0\.0\.53' <<<"$port53_users"; then
    warn "Something other than systemd-resolved is listening on port 53:"
    warn "$port53_users"
    warn "This script will not help. Investigate and stop that service first."
    exit 1
  fi
fi

# Already disabled? Nothing to do.
if grep -RqsE '^\s*DNSStubListener\s*=\s*no' /etc/systemd/resolved.conf /etc/systemd/resolved.conf.d 2>/dev/null; then
  if ! sudo ss -tulnp 2>/dev/null | grep -q '127\.0\.0\.53:53'; then
    log "Stub listener is already disabled and 127.0.0.53:53 is free."
    log "Nothing to do. ✅"
    exit 0
  fi
fi

# --- Drop-in ------------------------------------------------------------------

log "[2/6] Writing systemd-resolved drop-in to ${DROP_IN_FILE}..."

sudo mkdir -p "$DROP_IN_DIR"
sudo tee "$DROP_IN_FILE" >/dev/null <<'EOF'
# Disable the 127.0.0.53 stub listener so a containerised DNS server
# (e.g. dnsmasq for *.localhost wildcards) can bind port 53 on the
# loopback interface. systemd-resolved still runs and still resolves
# names for the host — /etc/resolv.conf just has to point at the
# real upstream config instead of the stub.
#
# Managed by docker-demo/solve-local-dns.sh. To revert:
#   sudo rm /etc/systemd/resolved.conf.d/00-disable-stub-listener.conf
#   sudo systemctl restart systemd-resolved
#   sudo ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
[Resolve]
DNSStubListener=no
EOF

# --- Restart ------------------------------------------------------------------

log "[3/6] Restarting systemd-resolved..."
sudo systemctl restart systemd-resolved

# Give resolved a moment to release the socket.
for _ in 1 2 3 4 5; do
  if ! sudo ss -tulnp 2>/dev/null | grep -q '127\.0\.0\.53:53'; then
    break
  fi
  sleep 0.5
done

# --- Rewire /etc/resolv.conf -------------------------------------------------

log "[4/6] Pointing /etc/resolv.conf at ${RESOLV_REAL}..."

# Capture the current state so rollback is possible.
if [[ -L "$RESOLV_CONF" ]]; then
  current_target=$(readlink "$RESOLV_CONF")
  log "  (current symlink target: $current_target)"
elif [[ -f "$RESOLV_CONF" ]]; then
  backup="${RESOLV_CONF}.backup.$(date +%s)"
  log "  (resolv.conf is a real file — backing up to $backup)"
  sudo cp -a "$RESOLV_CONF" "$backup"
fi

if [[ ! -e "$RESOLV_REAL" ]]; then
  warn "$RESOLV_REAL does not exist yet. systemd-resolved may not have"
  warn "finished writing it. Waiting 2s and retrying..."
  sleep 2
fi

if [[ ! -e "$RESOLV_REAL" ]]; then
  err "$RESOLV_REAL still missing. Aborting before breaking DNS."
  err "Your /etc/resolv.conf has not been changed."
  exit 1
fi

sudo ln -sf "$RESOLV_REAL" "$RESOLV_CONF"

# --- Verify -------------------------------------------------------------------

log "[5/6] Verifying port 53 is free on 127.0.0.53..."

if sudo ss -tulnp 2>/dev/null | grep -q '127\.0\.0\.53:53'; then
  err "FAILED: 127.0.0.53:53 is still occupied."
  err "Drop-in was written, but something else is holding the port."
  err "Current listeners:"
  sudo ss -tulnp 2>/dev/null | grep ':53 ' >&2 || true
  exit 1
fi
log "  ✓ 127.0.0.53:53 is free."

log "[6/6] Sanity check: can we still resolve names?"
if getent hosts cloudflare.com >/dev/null 2>&1; then
  log "  ✓ Name resolution still works."
else
  warn "  ⚠ Name resolution failed for cloudflare.com."
  warn "  resolv.conf contents:"
  cat "$RESOLV_CONF" | sed 's/^/    /' >&2
  warn "  You may need to check your upstream DNS config in resolved.conf."
fi

echo
log "Done. ✅"
echo
echo "Current state:"
echo "  /etc/resolv.conf  → $(readlink -f "$RESOLV_CONF" 2>/dev/null || echo "$RESOLV_CONF")"
echo "  drop-in           → $DROP_IN_FILE"
echo
echo "To roll back:"
echo "  sudo rm $DROP_IN_FILE"
echo "  sudo systemctl restart systemd-resolved"
echo "  sudo ln -sf $RESOLV_STUB /etc/resolv.conf"
echo
echo "Note: NetworkManager and some netplan configs will overwrite"
echo "/etc/resolv.conf on reboot or network-change events. If the stub"
echo "listener comes back, the drop-in above will still prevent it — you"
echo "just need to re-run \`sudo ln -sf $RESOLV_REAL /etc/resolv.conf\`."
