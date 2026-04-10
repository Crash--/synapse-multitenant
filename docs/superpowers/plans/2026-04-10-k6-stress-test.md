# k6 Multi-Tenant Stress Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a k6 stress test suite in `docker-demo/stress-test/` that validates multi-tenant correctness and performance under concurrent load across 4 tenants.

**Architecture:** A Python setup script bootstraps users and rooms, emits a JSON data file, then k6 runs a ramp-up load test consuming that data. An orchestrator shell script ties both together.

**Tech Stack:** k6 (JavaScript), Python 3 (setup), bash (orchestrator)

---

## File Structure

```
docker-demo/stress-test/
  setup.py          # Bootstrap users + rooms, emit test-data.json
  stress-test.js    # k6 load test script
  run.sh            # Orchestrator
  .gitignore        # Ignore test-data.json
```

**Spec:** `docs/superpowers/specs/2026-04-10-k6-multitenant-stress-test-design.md`

---

### Task 1: Update docker-compose to initialize all 4 tenants

The keygen and init-schemas services currently only handle tenants a and b. We need all 4 tenants bootstrapped for the stress test to work.

**Files:**
- Modify: `docker-demo/docker-compose.yml` (lines 110, 132 — TENANTS env vars)

- [ ] **Step 1: Update keygen TENANTS env var**

In `docker-demo/docker-compose.yml`, change the keygen service's TENANTS to include all 4:

```yaml
      TENANTS: "localhost,matrix.tenant-a.com,matrix.tenant-b.com,matrix.tenant-c.com,matrix.tenant-d.com"
```

Note: The `generate_keys.py` script uses `tenant.replace('.', '_')` which produces `matrix_tenant-c_com` (preserving no hyphen since there isn't one). For `matrix.tenant-c.com` this gives `matrix_tenant_c_com` which matches the `signing_key_path` in homeserver.yaml. Same for tenant-d: `matrix.tenant-d.com` → `matrix_tenant-d_com` but homeserver.yaml expects `matrix_tenant_d_com`. **Check:** `"matrix.tenant-d.com".replace(".", "_")` = `"matrix_tenant-d_com"` but homeserver.yaml line 53 has `matrix_tenant_d_com` (no hyphen). Same discrepancy exists for tenant-c: the script would produce `matrix_tenant-c_com` but yaml says `matrix_tenant_c_com`.

**Fix:** The homeserver.yaml key paths for c/d don't match the generate_keys.py naming convention. Update homeserver.yaml to be consistent:

In `docker-demo/config/homeserver.yaml`, change:
```yaml
    signing_key_path: /keys/matrix_tenant_c_com.signing.key
```
to:
```yaml
    signing_key_path: /keys/matrix_tenant-c_com.signing.key
```

And change:
```yaml
    signing_key_path: /keys/matrix_tenant_d_com.signing.key
```
to:
```yaml
    signing_key_path: /keys/matrix_tenant-d_com.signing.key
```

- [ ] **Step 2: Update init-schemas TENANTS env var**

In `docker-demo/docker-compose.yml`, change the init-schemas service's TENANTS:

```yaml
      TENANTS: "matrix.tenant-a.com,matrix.tenant-b.com,matrix.tenant-c.com,matrix.tenant-d.com"
```

- [ ] **Step 3: Add shared secrets for tenants c and d**

In `docker-demo/config/homeserver.yaml`, add shared secrets to the tenant-c and tenant-d blocks so the setup script can register users via the admin API. Update the tenant-c block:

```yaml
  - server_name: matrix.tenant-c.com
    database_schema: tenant_matrix_tenant_c_com
    signing_key_path: /keys/matrix_tenant-c_com.signing.key
    media_store_path: /media/matrix.tenant-c.com
    public_baseurl: http://matrix.tenant-c.com/
    registration_enabled: true
    registration_shared_secret: tenant_c_shared_secret_demo
    macaroon_secret_key: tenant_c_macaroon_demo
    form_secret: tenant_c_form_demo
    enable_federation: false
```

And tenant-d:

```yaml
  - server_name: matrix.tenant-d.com
    database_schema: tenant_matrix_tenant_d_com
    signing_key_path: /keys/matrix_tenant-d_com.signing.key
    media_store_path: /media/matrix.tenant-d.com
    public_baseurl: http://matrix.tenant-d.com/
    registration_enabled: true
    registration_shared_secret: tenant_d_shared_secret_demo
    macaroon_secret_key: tenant_d_macaroon_demo
    form_secret: tenant_d_form_demo
    enable_federation: false
```

- [ ] **Step 4: Commit**

```bash
git add docker-demo/docker-compose.yml docker-demo/config/homeserver.yaml
git commit -m "chore(docker-demo): bootstrap all 4 tenants with keys, schemas, and secrets"
```

---

### Task 2: Create the stress-test directory and .gitignore

**Files:**
- Create: `docker-demo/stress-test/.gitignore`

- [ ] **Step 1: Create directory and .gitignore**

```
test-data.json
```

- [ ] **Step 2: Commit**

```bash
git add docker-demo/stress-test/.gitignore
git commit -m "chore: scaffold stress-test directory"
```

---

### Task 3: Create setup.py — tenant bootstrap script

This script registers users and creates rooms on all 4 tenants, then writes `test-data.json` for k6.

**Files:**
- Create: `docker-demo/stress-test/setup.py`

**Reference:** See `docker-demo/scripts/test_tenants.py` lines 78-121 for the shared-secret registration pattern used in this codebase.

- [ ] **Step 1: Write setup.py**

```python
#!/usr/bin/env python3
"""Bootstrap users and rooms for the k6 stress test.

Registers 10 users per tenant, creates one room per tenant with all
users joined, and writes test-data.json for k6 to consume.

Usage:
    python3 setup.py [--host localhost] [--port 80]
"""

import argparse
import hashlib
import hmac
import json
import sys
import time

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", "requests"]
    )
    import requests

TENANTS = {
    "matrix.tenant-a.com": "tenant_a_shared_secret_demo",
    "matrix.tenant-b.com": "tenant_b_shared_secret_demo",
    "matrix.tenant-c.com": "tenant_c_shared_secret_demo",
    "matrix.tenant-d.com": "tenant_d_shared_secret_demo",
}

USERS_PER_TENANT = 10
USER_PASSWORD = "stresstest"


def _headers(tenant, token=None):
    h = {"Host": tenant, "Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def wait_for_synapse(base, tenants, max_retries=30):
    """Wait until Synapse responds on all tenant Host headers."""
    print("Waiting for Synapse to be ready on all tenants...")
    for tenant in tenants:
        for i in range(max_retries):
            try:
                r = requests.get(
                    f"{base}/_matrix/client/versions",
                    headers=_headers(tenant),
                    timeout=5,
                )
                if r.status_code == 200:
                    print(f"  [OK] {tenant}")
                    break
            except requests.ConnectionError:
                pass
            time.sleep(2)
        else:
            print(f"  [FAIL] {tenant} not ready after {max_retries * 2}s",
                  file=sys.stderr)
            sys.exit(1)


def register_user(base, tenant, username, password, shared_secret):
    """Register a user via the shared-secret admin endpoint.

    Returns access_token or None.
    """
    # Get nonce
    r = requests.get(
        f"{base}/_synapse/admin/v1/register",
        headers=_headers(tenant),
        timeout=10,
    )
    if r.status_code != 200:
        print(f"    [ERROR] nonce request failed: {r.status_code}", file=sys.stderr)
        return None
    nonce = r.json()["nonce"]

    # Build HMAC
    mac = hmac.new(
        shared_secret.encode("utf-8"),
        digestmod=hashlib.sha1,
    )
    mac.update(nonce.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(username.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(password.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(b"notadmin")

    body = {
        "nonce": nonce,
        "username": username,
        "password": password,
        "admin": False,
        "mac": mac.hexdigest(),
    }
    r = requests.post(
        f"{base}/_synapse/admin/v1/register",
        headers=_headers(tenant),
        json=body,
        timeout=10,
    )
    if r.status_code in (200, 201):
        return r.json().get("access_token")
    # User already exists — try logging in instead
    if r.status_code == 400 and r.json().get("errcode") == "M_USER_IN_USE":
        return login_user(base, tenant, username, password)
    print(f"    [ERROR] register {username}@{tenant}: {r.status_code} {r.text}",
          file=sys.stderr)
    return None


def login_user(base, tenant, username, password):
    """Login and return access_token."""
    body = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": username},
        "password": password,
    }
    r = requests.post(
        f"{base}/_matrix/client/v3/login",
        headers=_headers(tenant),
        json=body,
        timeout=10,
    )
    if r.status_code == 200:
        return r.json().get("access_token")
    return None


def create_room(base, tenant, token, room_alias_suffix):
    """Create a room and return its room_id."""
    body = {
        "preset": "public_chat",
        "name": f"Stress Test Room ({tenant})",
        "room_alias_name": room_alias_suffix,
    }
    r = requests.post(
        f"{base}/_matrix/client/v3/createRoom",
        headers=_headers(tenant, token),
        json=body,
        timeout=10,
    )
    if r.status_code == 200:
        return r.json()["room_id"]
    # Room alias may already exist — look it up
    if r.status_code == 400 and "M_ROOM_IN_USE" in r.text:
        return resolve_room_alias(base, tenant, token, room_alias_suffix)
    print(f"    [ERROR] createRoom on {tenant}: {r.status_code} {r.text}",
          file=sys.stderr)
    return None


def resolve_room_alias(base, tenant, token, alias_suffix):
    """Resolve a room alias to a room_id."""
    alias = f"%23{alias_suffix}%3A{tenant}"
    r = requests.get(
        f"{base}/_matrix/client/v3/directory/room/{alias}",
        headers=_headers(tenant, token),
        timeout=10,
    )
    if r.status_code == 200:
        return r.json()["room_id"]
    return None


def join_room(base, tenant, token, room_id):
    """Join a user to a room."""
    r = requests.post(
        f"{base}/_matrix/client/v3/join/{room_id}",
        headers=_headers(tenant, token),
        json={},
        timeout=10,
    )
    return r.status_code == 200


def main():
    parser = argparse.ArgumentParser(description="Bootstrap k6 stress test data")
    parser.add_argument("--host", default="localhost",
                        help="Traefik hostname (default: localhost)")
    parser.add_argument("--port", default="80",
                        help="Traefik port (default: 80)")
    args = parser.parse_args()

    base = f"http://{args.host}"
    if args.port != "80":
        base = f"http://{args.host}:{args.port}"

    wait_for_synapse(base, TENANTS.keys())

    test_data = {"tenants": {}}

    for tenant, shared_secret in TENANTS.items():
        print(f"\n--- {tenant} ---")
        tenant_data = {"users": [], "room_id": None}

        # Register users
        for i in range(USERS_PER_TENANT):
            username = f"user{i}"
            print(f"  Registering {username}...", end=" ")
            token = register_user(base, tenant, username, USER_PASSWORD, shared_secret)
            if token:
                print("OK")
                tenant_data["users"].append({
                    "username": username,
                    "password": USER_PASSWORD,
                    "access_token": token,
                })
            else:
                print("FAILED")
                sys.exit(1)

        # Create room via user0
        print(f"  Creating stress-test room...", end=" ")
        room_id = create_room(
            base, tenant, tenant_data["users"][0]["access_token"], "stress-test"
        )
        if room_id:
            print(f"OK ({room_id})")
            tenant_data["room_id"] = room_id
        else:
            print("FAILED")
            sys.exit(1)

        # Join all other users to the room
        for i in range(1, USERS_PER_TENANT):
            user = tenant_data["users"][i]
            print(f"  Joining {user['username']}...", end=" ")
            if join_room(base, tenant, user["access_token"], room_id):
                print("OK")
            else:
                print("FAILED")
                sys.exit(1)

        test_data["tenants"][tenant] = tenant_data

    # Write test-data.json
    output_path = "test-data.json"
    with open(output_path, "w") as f:
        json.dump(test_data, f, indent=2)
    print(f"\nTest data written to {output_path}")
    print(f"Total: {len(TENANTS)} tenants, {USERS_PER_TENANT} users each")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify setup.py runs against a live stack**

Run (with docker-compose stack already up):

```bash
cd docker-demo/stress-test
python3 setup.py --host localhost --port 80
```

Expected: all 40 users registered, 4 rooms created, `test-data.json` written.

- [ ] **Step 3: Commit**

```bash
git add docker-demo/stress-test/setup.py
git commit -m "feat(stress-test): add setup.py to bootstrap users and rooms"
```

---

### Task 4: Create stress-test.js — k6 load test script

**Files:**
- Create: `docker-demo/stress-test/stress-test.js`

**Key k6 concepts used:**
- `SharedArray` to load JSON data once across all VUs
- `__VU` and `__ITER` for deterministic VU-to-tenant mapping
- `check()` for inline assertions
- `Trend` for custom per-tenant metrics
- `stages` for ramp-up profile

- [ ] **Step 1: Write stress-test.js**

```javascript
import http from "k6/http";
import { check, sleep } from "k6";
import { SharedArray } from "k6/data";
import { Trend, Rate } from "k6/metrics";

// ── Load test data ─────────────────────────────────────────────
const testData = JSON.parse(open("./test-data.json"));
const tenantNames = Object.keys(testData.tenants);

// ── Custom metrics ─────────────────────────────────────────────
const messageSendDuration = new Trend("message_send_duration", true);
const timelineReadDuration = new Trend("timeline_read_duration", true);
const isolationCheckRate = new Rate("isolation_check_passed");

// ── Options ────────────────────────────────────────────────────
export const options = {
  stages: [
    { duration: "30s", target: 10 },  // warm-up
    { duration: "1m", target: 40 },   // ramp to full load
    { duration: "2m", target: 40 },   // sustained peak
    { duration: "30s", target: 0 },   // cool-down
  ],
  thresholds: {
    http_req_duration: ["p(95)<2000"],
    http_req_failed: ["rate<0.05"],
    checks: ["rate>0.95"],
  },
};

const BASE_URL = __ENV.BASE_URL || "http://localhost";

// ── Per-VU state ───────────────────────────────────────────────
let cachedToken = null;
let txnCounter = 0;

// ── VU assignment ──────────────────────────────────────────────
function getAssignment() {
  // VU IDs are 1-based in k6
  const vuId = __VU - 1;
  const tenantIndex = vuId % tenantNames.length;
  const userIndex = Math.floor(vuId / tenantNames.length) % 10;
  const tenantName = tenantNames[tenantIndex];
  const tenant = testData.tenants[tenantName];
  const user = tenant.users[userIndex];
  return { tenantName, tenant, user };
}

function headers(tenantName, token) {
  const h = {
    Host: tenantName,
    "Content-Type": "application/json",
  };
  if (token) {
    h["Authorization"] = `Bearer ${token}`;
  }
  return h;
}

// ── Login (cached per VU) ──────────────────────────────────────
function ensureLoggedIn(tenantName, user) {
  if (cachedToken) return cachedToken;

  const res = http.post(
    `${BASE_URL}/_matrix/client/v3/login`,
    JSON.stringify({
      type: "m.login.password",
      identifier: { type: "m.id.user", user: user.username },
      password: user.password,
    }),
    { headers: headers(tenantName) }
  );

  check(res, {
    "login succeeded": (r) => r.status === 200,
  });

  if (res.status === 200) {
    cachedToken = res.json().access_token;
  }
  return cachedToken;
}

// ── Main test function ─────────────────────────────────────────
export default function () {
  const { tenantName, tenant, user } = getAssignment();
  const token = ensureLoggedIn(tenantName, user);

  if (!token) {
    console.error(`VU ${__VU}: login failed for ${user.username}@${tenantName}`);
    sleep(1);
    return;
  }

  const roomId = tenant.room_id;
  const txnId = `k6_${__VU}_${txnCounter++}`;

  // ── Send message ───────────────────────────────────────────
  const sendRes = http.put(
    `${BASE_URL}/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/send/m.room.message/${txnId}`,
    JSON.stringify({
      msgtype: "m.text",
      body: `stress test msg from ${user.username}@${tenantName} iter=${__ITER}`,
    }),
    { headers: headers(tenantName, token) }
  );

  check(sendRes, {
    "message sent (200)": (r) => r.status === 200,
  });
  messageSendDuration.add(sendRes.timings.duration, { tenant: tenantName });

  // ── Read timeline ──────────────────────────────────────────
  const timelineRes = http.get(
    `${BASE_URL}/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/messages?dir=b&limit=10`,
    { headers: headers(tenantName, token) }
  );

  check(timelineRes, {
    "timeline read (200)": (r) => r.status === 200,
  });
  timelineReadDuration.add(timelineRes.timings.duration, { tenant: tenantName });

  // ── Verify no cross-tenant leak in timeline ────────────────
  if (timelineRes.status === 200) {
    const events = timelineRes.json().chunk || [];
    const allOwnTenant = events.every((evt) => {
      // sender format: @username:server_name
      const sender = evt.sender || "";
      return sender.endsWith(`:${tenantName}`);
    });
    check(null, {
      "no cross-tenant leak in timeline": () => allOwnTenant,
    });
  }

  // ── Isolation check (every 10th iteration) ─────────────────
  if (__ITER % 10 === 9) {
    // Pick a different tenant's room and try to read it
    const otherIndex = (tenantNames.indexOf(tenantName) + 1) % tenantNames.length;
    const otherTenantName = tenantNames[otherIndex];
    const otherRoomId = testData.tenants[otherTenantName].room_id;

    const isolationRes = http.get(
      `${BASE_URL}/_matrix/client/v3/rooms/${encodeURIComponent(otherRoomId)}/messages?dir=b&limit=1`,
      { headers: headers(tenantName, token) }
    );

    const isolated = isolationRes.status === 403 || isolationRes.status === 404;
    check(isolationRes, {
      "cross-tenant room blocked (403/404)": (r) => r.status === 403 || r.status === 404,
    });
    isolationCheckRate.add(isolated ? 1 : 0);
  }

  sleep(0.5 + Math.random() * 0.5); // 0.5–1s think time
}
```

- [ ] **Step 2: Commit**

```bash
git add docker-demo/stress-test/stress-test.js
git commit -m "feat(stress-test): add k6 load test script with isolation checks"
```

---

### Task 5: Create run.sh — orchestrator script

**Files:**
- Create: `docker-demo/stress-test/run.sh`

- [ ] **Step 1: Write run.sh**

```bash
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
```

- [ ] **Step 2: Make executable**

```bash
chmod +x docker-demo/stress-test/run.sh
```

- [ ] **Step 3: Commit**

```bash
git add docker-demo/stress-test/run.sh
git commit -m "feat(stress-test): add run.sh orchestrator"
```

---

### Task 6: Smoke test the full pipeline

- [ ] **Step 1: Rebuild the docker image (if needed)**

```bash
cd /home/monta/Documents/workspace/synapse-multitenant
DOCKER_BUILDKIT=1 docker build -t synapse:multi-tenant -f docker/Dockerfile .
```

- [ ] **Step 2: Bring up the stack fresh**

```bash
cd docker-demo
docker compose down -v
docker compose up -d
```

Wait for Synapse to be healthy:

```bash
docker compose logs -f synapse 2>&1 | head -100
```

- [ ] **Step 3: Run the stress test**

```bash
cd docker-demo/stress-test
./run.sh
```

Expected output:
- Phase 1: 40 users registered, 4 rooms created, test-data.json written
- Phase 2: k6 runs through all 4 stages, summary shows p95 < 2s, checks > 95%, no cross-tenant leaks

- [ ] **Step 4: Final commit if any fixes were needed**

```bash
git add -A docker-demo/stress-test/
git commit -m "fix(stress-test): adjustments from smoke test run"
```
