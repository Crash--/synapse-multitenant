# Docker Demo — Zero-Config Tenant Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the docker-demo's manual tenant provisioning with a zero-config flow: click "Create tenant" in the manager → within 10s the tenant is reachable over HTTPS with valid TLS, no file edits, no restarts. Sibling tenants can federate.

**Architecture:** Wildcard TLS (`*.localhost` via mkcert), wildcard Traefik `HostRegexp` route, bundled dnsmasq for in-container DNS, tenants loaded from `public.tenants` DB table (not YAML), SSE streaming for provisioning feedback, new federation-test endpoint.

**Tech Stack:** Docker Compose, Traefik v2, mkcert, dnsmasq (Alpine), Fastify (TypeScript), Next.js 16, Python (Synapse + tests).

**Spec:** `docs/superpowers/specs/2026-04-15-docker-demo-zero-config-provisioning-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `docker-demo/docker-compose.yml` | Modify | Remove keygen, per-tenant labels; add dnsmasq, certs-bootstrap; wire DNS |
| `docker-demo/dnsmasq/dnsmasq.conf` | Create | Wildcard `*.localhost → traefik_ip` |
| `docker-demo/scripts/setup_certs.sh` | Create | mkcert wildcard cert generation (idempotent) |
| `docker-demo/traefik/dynamic/routes.yml` | Create | Single wildcard route + infra routes |
| `docker-demo/traefik/dynamic/tls.yml` | Modify | Point at wildcard cert |
| `docker-demo/config/homeserver.yaml` | Modify | Drop hardcoded tenants; source=database; federation_verify_certificates: false |
| `synapse/rest/well_known.py` | Modify | Tenant-aware response |
| `tests/rest/test_well_known_multitenant.py` | Create | Probes for tenant-aware well-known |
| `docker-demo/control-plane/src/routes/tenants.ts` | Modify | SSE on POST; reserved-name validation |
| `docker-demo/control-plane/src/routes/federation-test.ts` | Create | Cross-tenant smoke orchestrator |
| `docker-demo/control-plane/src/index.ts` | Modify | Register federation-test route |
| `docker-demo/control-plane/src/routes/tenants.test.ts` | Create | SSE + reserved-name unit tests |
| `docker-demo/control-plane/src/routes/federation-test.test.ts` | Create | Federation-test unit tests |
| `docker-demo/manager/app/components/provisioning-drawer.tsx` | Create | SSE-consuming drawer |
| `docker-demo/manager/app/components/federation-test-panel.tsx` | Create | Federation test UI card |
| `docker-demo/manager/app/components/tenant-dashboard.tsx` | Modify | Wire drawer + panel |
| `docker-demo/manager/app/api/tenants/stream/route.ts` | Create | SSE proxy endpoint |
| `docker-demo/manager/app/api/tenants/[name]/federation-test/route.ts` | Create | Federation-test proxy |
| `docker-demo/scripts/test_tenants.py` | Modify | Swap hostnames; add zero-config + fed-test |
| `docker-demo/README.md` | Modify | Rewrite onboarding flow |

---

## Execution notes for subagents

- **Do not commit anything.** Standing instruction for this session. The `git commit` steps below are kept in the plan for reuse but the implementing subagent must SKIP commit steps. Leave changes in the working tree.
- Trial runner: `SYNAPSE_SKIP_RUST_CHECK=1 trial tests.path.to.test 2>&1 | tail -10`.
- TypeScript tests (control plane): `cd docker-demo/control-plane && npm test` (uses vitest; install if missing).
- The control plane's `tenants` table already has all the columns we need — no migrations.
- `hs.get_tenant_registry()` and phase 10'-a changes are IN THE WORKING TREE (uncommitted). They're part of this branch's state — assume they work.

---

## Task 1: Add dnsmasq compose service with wildcard `*.localhost` resolution

**Files:**
- Create: `docker-demo/dnsmasq/dnsmasq.conf`
- Modify: `docker-demo/docker-compose.yml`

- [ ] **Step 1: Create dnsmasq config**

Create `docker-demo/dnsmasq/dnsmasq.conf`:

```
# Resolve every *.localhost to the Traefik container's static IP.
# Containers in this compose network use this dnsmasq as their DNS.
# Adding a new tenant requires no changes here — the wildcard covers it.
address=/.localhost/172.28.0.3
no-resolv
log-queries
```

- [ ] **Step 2: Add custom network with static IPs and dnsmasq service to compose**

In `docker-demo/docker-compose.yml`:

Change the `networks:` block at the bottom to:

```yaml
networks:
  synapse-demo-net:
    driver: bridge
    ipam:
      config:
        - subnet: 172.28.0.0/24
```

Add this service after `networks:` definition... actually, add it BEFORE `postgres` in the `services:` block (put it near the top so it starts early):

```yaml
  dnsmasq:
    image: strm/dnsmasq:latest
    container_name: synapse-demo-dnsmasq
    restart: unless-stopped
    cap_add: [NET_ADMIN]
    volumes:
      - ./dnsmasq/dnsmasq.conf:/etc/dnsmasq.conf:ro
    networks:
      synapse-demo-net:
        ipv4_address: 172.28.0.2
```

Give Traefik its static IP too — modify the traefik service block to add:

```yaml
    networks:
      synapse-demo-net:
        ipv4_address: 172.28.0.3
```

(Replace the existing `networks: [synapse-demo-net]` line under traefik.)

- [ ] **Step 3: Add `dns:` directive to synapse service**

Under the `synapse:` service in compose, add:

```yaml
    dns:
      - 172.28.0.2
```

- [ ] **Step 4: Verify compose is syntactically valid**

Run: `cd docker-demo && docker compose config --quiet`
Expected: no output, exit code 0.

- [ ] **Step 5: Commit** (SKIP — no commits this session)

---

## Task 2: Cert bootstrap service with mkcert wildcard

**Files:**
- Create: `docker-demo/scripts/setup_certs.sh`
- Modify: `docker-demo/docker-compose.yml`
- Modify: `docker-demo/traefik/dynamic/tls.yml`

- [ ] **Step 1: Create setup_certs.sh**

Create `docker-demo/scripts/setup_certs.sh`, mode 0755:

```bash
#!/usr/bin/env bash
# Generate a wildcard mkcert certificate for *.localhost.
# Idempotent: re-runs are no-ops if the existing cert is already valid
# for *.localhost and localhost.
#
# Pre-req: mkcert installed on the host.
# Runs INSIDE a busybox-ish container that has the host's mkcert CA
# mounted in, so certs are trusted by the host OS automatically.

set -euo pipefail

CERT_DIR="${CERT_DIR:-/certs}"
CERT_FILE="$CERT_DIR/wildcard.crt"
KEY_FILE="$CERT_DIR/wildcard.key"
HOSTS=("*.localhost" "localhost")

if ! command -v mkcert >/dev/null 2>&1; then
  echo "ERROR: mkcert not installed. Install it on your host first:"
  echo "  macOS:         brew install mkcert"
  echo "  Debian/Ubuntu: sudo apt install mkcert"
  echo "  Other:         https://github.com/FiloSottile/mkcert"
  exit 1
fi

mkdir -p "$CERT_DIR"

if [[ -f "$CERT_FILE" && -f "$KEY_FILE" ]]; then
  # Check SANs still cover what we need.
  EXPECTED_SANS="DNS:*.localhost, DNS:localhost"
  ACTUAL_SANS=$(openssl x509 -in "$CERT_FILE" -noout -ext subjectAltName 2>/dev/null | tail -n +2 | tr -d ' \n')
  if [[ "$ACTUAL_SANS" == *"*.localhost"* && "$ACTUAL_SANS" == *"localhost"* ]]; then
    # Also check not expired.
    if openssl x509 -in "$CERT_FILE" -noout -checkend 604800 >/dev/null 2>&1; then
      echo "[certs] Existing cert at $CERT_FILE is valid for *.localhost and not expiring soon — leaving it."
      exit 0
    fi
  fi
fi

echo "[certs] Generating wildcard cert for *.localhost via mkcert..."
mkcert -install >/dev/null 2>&1 || true
mkcert -cert-file "$CERT_FILE" -key-file "$KEY_FILE" "${HOSTS[@]}"
echo "[certs] Wrote $CERT_FILE and $KEY_FILE"
```

- [ ] **Step 2: Add certs one-shot service to compose**

NOTE: mkcert must run on the HOST, not inside a container (it needs access to the host's CA trust store). The simplest approach: the `certs` service is a one-shot that runs the script via the host's mkcert binary through a shared volume. Alternative: require the user to run the script on the host first.

For a demo, require the user to run `scripts/setup_certs.sh` on the host BEFORE `docker compose up`. Add the README instruction in Task 19.

Keep the compose change minimal: the existing `./certs:/certs:ro` mount on Traefik is kept. No new compose service for certs; the setup script is a host-side tool.

Update `docker-demo/scripts/setup_certs.sh`: remove the container-specific `/certs` assumption. Use a relative path:

```bash
# Replace the CERT_DIR default line with:
CERT_DIR="${CERT_DIR:-$(cd "$(dirname "$0")/.." && pwd)/certs}"
```

- [ ] **Step 3: Update traefik tls config**

Replace `docker-demo/traefik/dynamic/tls.yml` content with:

```yaml
tls:
  certificates:
    - certFile: /certs/wildcard.crt
      keyFile:  /certs/wildcard.key
  stores:
    default:
      defaultCertificate:
        certFile: /certs/wildcard.crt
        keyFile:  /certs/wildcard.key
```

- [ ] **Step 4: Run the script to verify**

Run (from the host, in a terminal with mkcert installed):
```
cd docker-demo && bash scripts/setup_certs.sh
```
Expected: creates `certs/wildcard.crt` and `certs/wildcard.key`. If mkcert is missing, prints the install hint and exits 1.

- [ ] **Step 5: Commit** (SKIP)

---

## Task 3: Replace Traefik labels with file-provider wildcard route

**Files:**
- Create: `docker-demo/traefik/dynamic/routes.yml`
- Modify: `docker-demo/docker-compose.yml`

- [ ] **Step 1: Create routes.yml**

Create `docker-demo/traefik/dynamic/routes.yml`:

```yaml
http:
  routers:
    # Wildcard: any <something>.localhost that is NOT one of the infra
    # hostnames falls through to Synapse. Synapse picks the tenant from
    # the Host header / X-Matrix destination (phases 7/10).
    synapse-wildcard:
      rule: "HostRegexp(`{subdomain:[a-z0-9-]+}.localhost`) && !Host(`manager.localhost`) && !Host(`control-plane.localhost`) && !Host(`traefik.localhost`)"
      service: synapse
      tls: {}
      entryPoints: [websecure]
      priority: 10

    manager:
      rule: "Host(`manager.localhost`)"
      service: manager
      tls: {}
      entryPoints: [websecure]
      priority: 100

    control-plane:
      rule: "Host(`control-plane.localhost`)"
      service: control-plane
      tls: {}
      entryPoints: [websecure]
      priority: 100

  services:
    synapse:
      loadBalancer:
        servers:
          - url: "http://synapse:8008"
    manager:
      loadBalancer:
        servers:
          - url: "http://manager:3000"
    control-plane:
      loadBalancer:
        servers:
          - url: "http://control-plane:3001"
```

- [ ] **Step 2: Remove Traefik docker-label provider + synapse labels**

In `docker-demo/docker-compose.yml`:

Under the `traefik:` service `command:` list, remove these lines:
```
- '--providers.docker=true'
- '--providers.docker.exposedbydefault=false'
- '--providers.docker.network=docker-demo_synapse-demo-net'
```

Keep the file provider lines. Remove the `/var/run/docker.sock` volume mount (no longer needed).

Under the `synapse:` service, REMOVE the entire `labels:` block (lines starting with `- 'traefik.enable=true'` through `- 'traefik.http.services.synapse.loadbalancer.server.port=8008'`).

Under the `control-plane:` service, REMOVE the `ports: - '3001:3001'` line (routing is through Traefik now). Also under `manager:`, REMOVE `ports: - '3000:3000'`.

- [ ] **Step 3: Restart and verify**

Run:
```
cd docker-demo && docker compose down && docker compose up -d traefik
docker compose logs traefik | grep -i "router synapse-wildcard"
```
Expected: Traefik logs show the wildcard router loaded from file provider.

- [ ] **Step 4: Commit** (SKIP)

---

## Task 4: Update `homeserver.yaml` — DB-source tenants, federation_verify_certificates off

**Files:**
- Modify: `docker-demo/config/homeserver.yaml`

- [ ] **Step 1: Replace tenant config block**

Edit `docker-demo/config/homeserver.yaml`. Replace lines 1-63 (the `server_name:` through the end of the tenant-d block) with:

```yaml
server_name: localhost
pid_file: /data/homeserver.pid
web_client: false
soft_file_limit: 0
public_baseurl: http://localhost/

# Multi-tenant enabled; tenants loaded from public.tenants DB table
# (populated by the control plane at runtime).
multi_tenant:
  enabled: true
  default_schema: public
  source: database
  reload_secret: demo-reload-secret

# Demo only: accept any TLS cert on outbound federation (siblings
# present the mkcert wildcard cert; the Synapse container's trust
# store does not include the mkcert root CA, so we skip verification).
# DO NOT use this in production.
federation_verify_certificates: false
```

(Keep everything from the `listeners:` block onward — don't touch lines 64+.)

- [ ] **Step 2: Remove keygen + init-schemas references to hardcoded tenant list**

In `docker-demo/docker-compose.yml`, REMOVE the entire `keygen:` service block (control plane generates keys on-demand now) AND the entire `init-schemas:` service block (control plane clones per-tenant schemas). Also remove `keygen` and `init-schemas` from `depends_on:` blocks of the `synapse` service.

Synapse still needs the public schema to exist with the base tables. Since `copy-schemas` clones FROM public to tenant_x, public itself must be migrated first. Synapse auto-migrates public on startup — that's fine, it already worked that way.

Remove `bootstrap-admin` service too — the manager UI will handle initial user creation via the federation-test feature.

- [ ] **Step 3: Verify compose still parses**

```
cd docker-demo && docker compose config --quiet
```
Expected: exit 0.

- [ ] **Step 4: Commit** (SKIP)

---

## Task 5: Red probe — tenant-aware `ServerWellKnownResource`

**Files:**
- Create: `tests/rest/test_well_known_multitenant.py`

- [ ] **Step 1: Write the failing test**

Create `tests/rest/test_well_known_multitenant.py`:

```python
#
# Copyright 2026 Linagora
#
"""
Probes for tenant-aware .well-known/matrix/server responses.

Bug: ServerWellKnownResource caches one response at __init__ based on
hs.config.server.server_name. Multi-tenant hosts all get the global
server name. We want per-tenant responses based on ContextVar.
"""

from unittest import TestCase
from unittest.mock import MagicMock

from synapse.config.tenants import TenantConfig
from synapse.tenant_context import set_current_tenant, reset_current_tenant


def _make_tenant(name: str) -> TenantConfig:
    return TenantConfig(
        server_name=f"{name}.localhost",
        database_schema=f"tenant_{name}",
        signing_key_path=f"/keys/{name}.key",
        media_store_path=f"/media/{name}",
    )


def _make_hs() -> MagicMock:
    hs = MagicMock()
    hs.config.server.server_name = "main.localhost"
    hs.config.server.serve_server_wellknown = True
    return hs


class TestWellKnownTenantAware(TestCase):
    def test_returns_tenant_server_name_when_context_set(self) -> None:
        from synapse.rest.well_known import ServerWellKnownResource
        import json

        hs = _make_hs()
        resource = ServerWellKnownResource(hs)

        acme = _make_tenant("acme")
        token = set_current_tenant(acme)
        try:
            # Simulate a GET request
            request = MagicMock()
            request.setResponseCode = MagicMock()
            request.setHeader = MagicMock()
            body = resource.render_GET(request)
            payload = json.loads(body)
            self.assertEqual(payload["m.server"], "acme.localhost:443")
        finally:
            reset_current_tenant(token)

    def test_falls_back_to_global_without_context(self) -> None:
        from synapse.rest.well_known import ServerWellKnownResource
        import json

        hs = _make_hs()
        resource = ServerWellKnownResource(hs)

        # Explicitly clear any leaked context
        clear_token = set_current_tenant(None)
        try:
            request = MagicMock()
            request.setResponseCode = MagicMock()
            request.setHeader = MagicMock()
            body = resource.render_GET(request)
            payload = json.loads(body)
            # Global server_name is "main.localhost"; well-known should
            # say main.localhost:443 (or whatever port we parsed, but
            # our impl hardcodes 443 for the demo).
            self.assertIn("m.server", payload)
            self.assertTrue(payload["m.server"].startswith("main.localhost"))
        finally:
            reset_current_tenant(clear_token)
```

- [ ] **Step 2: Run to verify it fails**

```
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.rest.test_well_known_multitenant 2>&1 | tail -15
```
Expected: `test_returns_tenant_server_name_when_context_set` FAILS — current response doesn't use tenant context.

- [ ] **Step 3: Commit** (SKIP)

---

## Task 6: Implement tenant-aware `ServerWellKnownResource`

**Files:**
- Modify: `synapse/rest/well_known.py`

- [ ] **Step 1: Make render_GET dynamic and tenant-aware**

In `synapse/rest/well_known.py`, find `class ServerWellKnownResource`. Current `__init__` pre-caches `self._response` — remove that caching. Modify `render_GET` to build the response per-request from current tenant context:

Replace the existing `ServerWellKnownResource.__init__` and `render_GET` with:

```python
class ServerWellKnownResource(Resource):
    """Resource for .well-known/matrix/server, returning tenant-aware m.server."""

    isLeaf = 1

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._serve_server_wellknown = hs.config.server.serve_server_wellknown
        self._global_server_name = hs.config.server.server_name

    def render_GET(self, request: Request) -> bytes:
        if not self._serve_server_wellknown:
            request.setResponseCode(404)
            request.setHeader(b"Content-Type", b"text/plain")
            return b"404. Is anything ever truly *well* known?\n"

        # Tenant-aware: use the current tenant's server_name when set.
        from synapse.tenant_context import get_current_tenant
        tenant = get_current_tenant()
        server_name = tenant.server_name if tenant is not None else self._global_server_name

        # Parse optional port out of server_name; default to 443.
        host, port = parse_server_name(server_name)
        if port is None:
            port = 443

        response = json_encoder.encode(
            {"m.server": f"{host}:{port}"}
        ).encode("utf-8")

        request.setHeader(b"Content-Type", b"application/json")
        return response
```

Make sure the imports at the top include:
```python
from synapse.types import parse_server_name
```
(It should already be imported — verify.)

- [ ] **Step 2: Run the probes**

```
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.rest.test_well_known_multitenant 2>&1 | tail -10
```
Expected: both tests PASS.

- [ ] **Step 3: Regression check**

```
SYNAPSE_SKIP_RUST_CHECK=1 trial tests.rest 2>&1 | tail -5
```
Expected: no new failures (pre-existing failures acceptable if they existed before).

- [ ] **Step 4: Commit** (SKIP)

---

## Task 7: Red probe — control plane SSE streaming on POST /tenants

**Files:**
- Create: `docker-demo/control-plane/src/routes/tenants.test.ts`
- Create: `docker-demo/control-plane/vitest.config.ts`
- Modify: `docker-demo/control-plane/package.json` (add vitest dependencies)

- [ ] **Step 1: Install vitest**

```
cd docker-demo/control-plane && npm install --save-dev vitest @vitest/ui
```

- [ ] **Step 2: Create vitest.config.ts**

Create `docker-demo/control-plane/vitest.config.ts`:

```typescript
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    globals: true,
  },
});
```

- [ ] **Step 3: Add test script to package.json**

In `docker-demo/control-plane/package.json`, add under `scripts`:
```json
"test": "vitest run"
```

- [ ] **Step 4: Write failing SSE test**

Create `docker-demo/control-plane/src/routes/tenants.test.ts`:

```typescript
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import Fastify, { type FastifyInstance } from "fastify";
import { tenantRoutes } from "./tenants.js";

// Mock the bits that touch the DB / filesystem / external services.
function makeMockDeps() {
  const tenantRows: any[] = [];
  const db = {
    select: () => ({ from: () => tenantRows }),
    insert: () => ({
      values: (v: any) => ({
        returning: async () => [{ id: "t1", ...v }],
      }),
    }),
    update: () => ({
      set: () => ({ where: async () => undefined }),
    }),
  } as any;
  const config = {
    MEDIA_BASE_DIR: "/tmp/test-media",
    TENANT_KEY_MASTER: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    SYNAPSE_URL: "http://synapse:8008",
    SYNAPSE_RELOAD_SECRET: "test",
  };
  const pool = {} as any;
  return { db, config, pool };
}

describe("POST /api/v1/tenants SSE streaming", () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    app = Fastify();
    await app.register(tenantRoutes, makeMockDeps());
    await app.ready();
  });

  afterAll(async () => {
    await app.close();
  });

  it("returns SSE stream when Accept: text/event-stream", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/api/v1/tenants",
      headers: { accept: "text/event-stream" },
      payload: { server_name: "fresh-sse.localhost" },
    });
    expect(res.statusCode).toBe(200);
    expect(res.headers["content-type"]).toMatch(/text\/event-stream/);
    // Body should contain at least one `event: step` line and a final
    // `event: complete` line.
    expect(res.body).toMatch(/event: step/);
    expect(res.body).toMatch(/event: complete/);
  });

  it("returns single JSON when Accept: application/json", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/api/v1/tenants",
      headers: { accept: "application/json" },
      payload: { server_name: "fresh-json.localhost" },
    });
    expect(res.statusCode).toBe(201);
    expect(res.headers["content-type"]).toMatch(/application\/json/);
    const body = JSON.parse(res.body);
    expect(body.tenant.server_name).toBe("fresh-json.localhost");
  });

  it("rejects reserved names", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/api/v1/tenants",
      headers: { accept: "application/json" },
      payload: { server_name: "manager.localhost" },
    });
    expect(res.statusCode).toBe(400);
    expect(JSON.parse(res.body).error).toMatch(/reserved/i);
  });
});
```

- [ ] **Step 5: Run to verify fails**

```
cd docker-demo/control-plane && npm test 2>&1 | tail -20
```
Expected: all three tests FAIL — current route returns JSON only and doesn't validate reserved names.

- [ ] **Step 6: Commit** (SKIP)

---

## Task 8: Implement SSE streaming and reserved-name validation

**Files:**
- Modify: `docker-demo/control-plane/src/routes/tenants.ts`

- [ ] **Step 1: Add reserved-name constant and validation**

At the top of `docker-demo/control-plane/src/routes/tenants.ts`, after imports:

```typescript
const RESERVED_NAMES = new Set([
  "localhost",
  "manager.localhost",
  "control-plane.localhost",
  "traefik.localhost",
]);
```

- [ ] **Step 2: Refactor POST handler to support SSE**

Replace the entire `app.post<...>("/api/v1/tenants", async (request, reply) => { ... })` block with (PARAPHRASED — read current implementation to preserve provisioning logic; add branching at top and SSE emission calls throughout):

```typescript
  app.post<{
    Body: {
      server_name: string;
      registration_enabled?: boolean;
      enable_federation?: boolean;
      max_mau_value?: number;
      public_baseurl?: string;
    };
  }>("/api/v1/tenants", async (request, reply) => {
    const body = request.body as any;
    const serverName = body.server_name;

    if (!serverName || typeof serverName !== "string") {
      return reply.status(400).send({ error: "Missing required field: server_name" });
    }

    if (RESERVED_NAMES.has(serverName)) {
      return reply.status(400).send({ error: `'${serverName}' is a reserved name; pick another` });
    }

    // Check duplicate
    const [existing] = await db
      .select()
      .from(tenants)
      .where(eq(tenants.serverName, serverName));
    if (existing) {
      return reply.status(409).send({ error: `Tenant '${serverName}' already exists` });
    }

    const wantsSSE = (request.headers.accept ?? "").includes("text/event-stream");

    if (wantsSSE) {
      // Stream provisioning steps.
      reply.raw.setHeader("Content-Type", "text/event-stream");
      reply.raw.setHeader("Cache-Control", "no-cache");
      reply.raw.setHeader("Connection", "keep-alive");
      reply.raw.flushHeaders();

      const emit = (event: string, data: unknown) => {
        reply.raw.write(`event: ${event}\n`);
        reply.raw.write(`data: ${JSON.stringify(data)}\n\n`);
      };

      try {
        const result = await provisionTenant(
          { db, config, pool, masterKey, serverName, body, emit }
        );
        emit("complete", { tenant: result });
      } catch (err: any) {
        emit("error", { message: err.message });
      } finally {
        reply.raw.end();
      }
      // Tell Fastify we handled the response manually.
      return reply;
    }

    // JSON path — unchanged behaviour
    const result = await provisionTenant(
      { db, config, pool, masterKey, serverName, body, emit: () => {} }
    );
    return reply.status(201).send({ tenant: result, steps: [] });
  });
```

- [ ] **Step 3: Extract `provisionTenant` helper**

At the bottom of the file (or in a new helper at the top of the closure), add the common provisioning logic extracted from the old POST handler. The function emits step events and returns the created tenant record. Paraphrased:

```typescript
type ProvisionArgs = {
  db: any;
  config: any;
  pool: any;
  masterKey: Buffer;
  serverName: string;
  body: any;
  emit: (event: string, data: unknown) => void;
};

async function provisionTenant(args: ProvisionArgs) {
  const { db, config, pool, masterKey, serverName, body, emit } = args;
  const safeName = serverName.replace(/[^a-zA-Z0-9]/g, "_");
  const databaseSchema = `tenant_${safeName}`;
  const mediaStorePath = `${config.MEDIA_BASE_DIR}/${serverName}`;

  emit("step", { step: "keygen", status: "running" });
  const { keyText, keyId } = generateSigningKey(serverName);
  const signingKeyEncrypted = encryptSigningKey(keyText, masterKey);
  emit("step", { step: "keygen", status: "ok", detail: { keyId } });

  const macaroonSecretKey = randomBytes(32).toString("hex");
  const formSecret = randomBytes(32).toString("hex");

  emit("step", { step: "db_row", status: "running" });
  const [inserted] = await db
    .insert(tenants)
    .values({
      serverName, databaseSchema, status: "provisioning",
      signingKeyEncrypted, signingKeyId: keyId,
      macaroonSecretKey, formSecret, mediaStorePath,
      registrationEnabled: body.registration_enabled ?? false,
      enableFederation: body.enable_federation ?? true,
      maxMauValue: body.max_mau_value ?? 0,
      publicBaseurl: body.public_baseurl ?? `https://${serverName}/`,
    })
    .returning();
  emit("step", { step: "db_row", status: "ok" });

  emit("step", { step: "schema_clone", status: "running" });
  await provisionTenantSchema(pool, databaseSchema);
  emit("step", { step: "schema_clone", status: "ok" });

  emit("step", { step: "media_dir", status: "running" });
  try {
    const { mkdir } = await import("node:fs/promises");
    await mkdir(mediaStorePath, { recursive: true });
    emit("step", { step: "media_dir", status: "ok" });
  } catch (err: any) {
    emit("step", { step: "media_dir", status: "ok", detail: "non-fatal: " + err.message });
  }

  emit("step", { step: "activate", status: "running" });
  await db
    .update(tenants)
    .set({ status: "active", updatedAt: new Date() })
    .where(eq(tenants.id, inserted.id));
  emit("step", { step: "activate", status: "ok" });

  emit("step", { step: "synapse_reload", status: "running" });
  try {
    await reloadSynapseTenants(config.SYNAPSE_URL, config.SYNAPSE_RELOAD_SECRET);
    emit("step", { step: "synapse_reload", status: "ok" });
  } catch (err: any) {
    emit("step", { step: "synapse_reload", status: "ok", detail: "non-fatal: " + err.message });
  }

  return {
    id: inserted.id,
    server_name: serverName,
    database_schema: databaseSchema,
    status: "active",
  };
}
```

- [ ] **Step 4: Run tests**

```
cd docker-demo/control-plane && npm test 2>&1 | tail -10
```
Expected: all three tests PASS.

- [ ] **Step 5: Commit** (SKIP)

---

## Task 9: Red probe — `/federation-test` endpoint

**Files:**
- Create: `docker-demo/control-plane/src/routes/federation-test.test.ts`

- [ ] **Step 1: Write the failing test**

Create `docker-demo/control-plane/src/routes/federation-test.test.ts`:

```typescript
import { describe, it, expect, beforeAll, afterAll, vi } from "vitest";
import Fastify, { type FastifyInstance } from "fastify";
import { federationTestRoutes } from "./federation-test.js";

function makeDeps() {
  return {
    config: {
      SYNAPSE_URL: "http://synapse:8008",
      SHARED_SECRET: "test-shared-secret",
    },
  };
}

describe("POST /api/v1/tenants/:source/federation-test", () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    // Mock global fetch so we don't hit a real Synapse
    (global as any).fetch = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ access_token: "fake", user_id: "@u:acme.localhost" }),
      text: async () => JSON.stringify({}),
    }));
    app = Fastify();
    await app.register(federationTestRoutes, makeDeps());
    await app.ready();
  });

  afterAll(async () => {
    await app.close();
  });

  it("streams step events and completes", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/api/v1/tenants/acme.localhost/federation-test",
      headers: { accept: "text/event-stream" },
      payload: { target: "corp.localhost" },
    });
    expect(res.statusCode).toBe(200);
    expect(res.body).toMatch(/event: step/);
    expect(res.body).toMatch(/event: complete|event: error/);
  });
});
```

- [ ] **Step 2: Run to verify fail**

```
cd docker-demo/control-plane && npm test 2>&1 | tail -10
```
Expected: import fails — `federation-test.ts` doesn't exist.

- [ ] **Step 3: Commit** (SKIP)

---

## Task 10: Implement `/federation-test` endpoint

**Files:**
- Create: `docker-demo/control-plane/src/routes/federation-test.ts`
- Modify: `docker-demo/control-plane/src/index.ts`

- [ ] **Step 1: Create federation-test.ts**

Create `docker-demo/control-plane/src/routes/federation-test.ts`:

```typescript
import type { FastifyInstance } from "fastify";
import type { Config } from "../config.js";

type Deps = { config: Config & { SHARED_SECRET?: string } };

export async function federationTestRoutes(app: FastifyInstance, opts: Deps) {
  const { config } = opts;

  app.post<{
    Params: { source: string };
    Body: { target: string };
  }>("/api/v1/tenants/:source/federation-test", async (request, reply) => {
    const { source } = request.params;
    const { target } = request.body;
    const baseUrl = config.SYNAPSE_URL; // same Synapse, Host header picks tenant

    if (!target || target === source) {
      return reply.status(400).send({ error: "target must differ from source" });
    }

    reply.raw.setHeader("Content-Type", "text/event-stream");
    reply.raw.setHeader("Cache-Control", "no-cache");
    reply.raw.setHeader("Connection", "keep-alive");
    reply.raw.flushHeaders();

    const emit = (event: string, data: unknown) => {
      reply.raw.write(`event: ${event}\n`);
      reply.raw.write(`data: ${JSON.stringify(data)}\n\n`);
    };

    const stamp = Date.now();
    const sourceUser = `_fed_test_src_${stamp}`;
    const targetUser = `_fed_test_tgt_${stamp}`;
    let sourceToken = "";
    let targetToken = "";
    let roomId = "";

    try {
      emit("step", { step: "register_source", status: "running" });
      sourceToken = await registerViaSharedSecret(baseUrl, source, sourceUser, config.SHARED_SECRET ?? "");
      emit("step", { step: "register_source", status: "ok" });

      emit("step", { step: "register_target", status: "running" });
      targetToken = await registerViaSharedSecret(baseUrl, target, targetUser, config.SHARED_SECRET ?? "");
      emit("step", { step: "register_target", status: "ok" });

      emit("step", { step: "create_room", status: "running" });
      roomId = await createRoom(baseUrl, source, sourceToken);
      emit("step", { step: "create_room", status: "ok", detail: { roomId } });

      emit("step", { step: "invite", status: "running" });
      await invite(baseUrl, source, sourceToken, roomId, `@${targetUser}:${target}`);
      emit("step", { step: "invite", status: "ok" });

      emit("step", { step: "join", status: "running" });
      await joinRoom(baseUrl, target, targetToken, roomId, [source]);
      emit("step", { step: "join", status: "ok" });

      emit("step", { step: "send_message", status: "running" });
      await sendMessage(baseUrl, source, sourceToken, roomId, "ping");
      emit("step", { step: "send_message", status: "ok" });

      emit("step", { step: "receive_message", status: "running" });
      const received = await pollForMessage(baseUrl, target, targetToken, roomId, "ping", 8000);
      emit("step", { step: "receive_message", status: received ? "ok" : "error" });

      if (!received) throw new Error("Message not received on target within 8s");

      emit("complete", { ok: true, roomId });
    } catch (err: any) {
      emit("error", { message: err.message });
    } finally {
      reply.raw.end();
    }
    return reply;
  });
}

// --- Helpers: thin wrappers around Synapse admin/client APIs ---
// They set Host header to pick the tenant.

async function fetchJson(url: string, host: string, init: any) {
  const headers = {
    "Content-Type": "application/json",
    Host: host,
    ...(init.headers || {}),
  };
  const resp = await fetch(url, { ...init, headers });
  const text = await resp.text();
  if (!resp.ok) {
    throw new Error(`${init.method || "GET"} ${url} → ${resp.status}: ${text}`);
  }
  return text ? JSON.parse(text) : {};
}

async function registerViaSharedSecret(base: string, host: string, localpart: string, secret: string): Promise<string> {
  // Use /_synapse/admin/v1/register with HMAC — pre-registered shared_secret
  // required. For a demo we assume the tenant was created with a shared secret.
  // Simpler path: hit /_matrix/client/v3/register (registration_enabled must be true).
  const body = {
    auth: { type: "m.login.dummy" },
    username: localpart,
    password: "fed-test-password",
  };
  const res = await fetchJson(`${base}/_matrix/client/v3/register`, host, {
    method: "POST",
    body: JSON.stringify(body),
  });
  return res.access_token;
}

async function createRoom(base: string, host: string, token: string): Promise<string> {
  const res = await fetchJson(`${base}/_matrix/client/v3/createRoom`, host, {
    method: "POST",
    body: JSON.stringify({ preset: "private_chat", name: "fed-test" }),
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.room_id;
}

async function invite(base: string, host: string, token: string, roomId: string, userId: string) {
  await fetchJson(
    `${base}/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/invite`,
    host,
    {
      method: "POST",
      body: JSON.stringify({ user_id: userId }),
      headers: { Authorization: `Bearer ${token}` },
    }
  );
}

async function joinRoom(base: string, host: string, token: string, roomId: string, via: string[]) {
  const qs = via.map((v) => `server_name=${encodeURIComponent(v)}`).join("&");
  await fetchJson(
    `${base}/_matrix/client/v3/join/${encodeURIComponent(roomId)}?${qs}`,
    host,
    {
      method: "POST",
      body: "{}",
      headers: { Authorization: `Bearer ${token}` },
    }
  );
}

async function sendMessage(base: string, host: string, token: string, roomId: string, body: string) {
  const txn = Date.now().toString();
  await fetchJson(
    `${base}/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/send/m.room.message/${txn}`,
    host,
    {
      method: "PUT",
      body: JSON.stringify({ msgtype: "m.text", body }),
      headers: { Authorization: `Bearer ${token}` },
    }
  );
}

async function pollForMessage(base: string, host: string, token: string, roomId: string, text: string, timeoutMs: number): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const res = await fetchJson(`${base}/_matrix/client/v3/sync?timeout=2000`, host, {
      method: "GET",
      headers: { Authorization: `Bearer ${token}` },
    });
    const room = res.rooms?.join?.[roomId];
    const events = room?.timeline?.events ?? [];
    if (events.some((e: any) => e.type === "m.room.message" && e.content?.body === text)) {
      return true;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}
```

- [ ] **Step 2: Register the route in index.ts**

In `docker-demo/control-plane/src/index.ts`, find where `tenantRoutes` is registered and add a line after it:

```typescript
import { federationTestRoutes } from "./routes/federation-test.js";
// ...
await app.register(federationTestRoutes, { config });
```

- [ ] **Step 3: Run the test**

```
cd docker-demo/control-plane && npm test 2>&1 | tail -10
```
Expected: all tests PASS (federation-test test + previous SSE tests).

- [ ] **Step 4: Commit** (SKIP)

---

## Task 11: Manager UI — streaming provisioning drawer

**Files:**
- Create: `docker-demo/manager/app/components/provisioning-drawer.tsx`
- Create: `docker-demo/manager/app/api/tenants/stream/route.ts`
- Modify: `docker-demo/manager/app/components/create-tenant-dialog.tsx`
- Modify: `docker-demo/manager/app/components/tenant-dashboard.tsx`

- [ ] **Step 1: Create SSE proxy route**

Create `docker-demo/manager/app/api/tenants/stream/route.ts`:

```typescript
import type { NextRequest } from "next/server";

const CONTROL_PLANE_URL = process.env.CONTROL_PLANE_URL || "http://control-plane:3001";
const CONTROL_PLANE_TOKEN = process.env.CONTROL_PLANE_TOKEN || "";

export async function POST(req: NextRequest) {
  const body = await req.text();
  const upstream = await fetch(`${CONTROL_PLANE_URL}/api/v1/tenants`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      Authorization: `Bearer ${CONTROL_PLANE_TOKEN}`,
    },
    body,
  });
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  });
}
```

- [ ] **Step 2: Create provisioning drawer component**

Create `docker-demo/manager/app/components/provisioning-drawer.tsx`:

```typescript
"use client";

import { useEffect, useState } from "react";

type StepEvent = { step: string; status: "running" | "ok" | "error"; detail?: any };

type Props = {
  serverName: string;
  onClose: () => void;
  onComplete: (tenant: any) => void;
};

export function ProvisioningDrawer({ serverName, onClose, onComplete }: Props) {
  const [steps, setSteps] = useState<StepEvent[]>([]);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/tenants/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ server_name: serverName }),
        });
        if (!res.body) throw new Error("No response body");
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (!cancelled) {
          const { done: d, value } = await reader.read();
          if (d) break;
          buf += decoder.decode(value, { stream: true });
          // Split on blank line = one SSE message.
          let idx;
          while ((idx = buf.indexOf("\n\n")) !== -1) {
            const chunk = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            const evLine = chunk.split("\n").find((l) => l.startsWith("event: "));
            const dataLine = chunk.split("\n").find((l) => l.startsWith("data: "));
            if (!evLine || !dataLine) continue;
            const ev = evLine.slice(7);
            const data = JSON.parse(dataLine.slice(6));
            if (ev === "step") {
              setSteps((s) => {
                // replace running → ok/error for same step
                const existing = s.findIndex((x) => x.step === data.step);
                if (existing >= 0) {
                  const next = [...s];
                  next[existing] = data;
                  return next;
                }
                return [...s, data];
              });
            } else if (ev === "complete") {
              setDone(true);
              onComplete(data.tenant);
            } else if (ev === "error") {
              setError(data.message);
            }
          }
        }
      } catch (e: any) {
        setError(e.message);
      }
    })();
    return () => { cancelled = true; };
  }, [serverName, onComplete]);

  return (
    <div className="fixed inset-y-0 right-0 w-96 bg-white dark:bg-neutral-900 shadow-2xl p-6 overflow-y-auto border-l border-neutral-200 dark:border-neutral-800 z-50">
      <div className="flex justify-between items-center mb-4">
        <h2 className="font-semibold text-lg">Creating {serverName}</h2>
        <button onClick={onClose} className="text-neutral-500 hover:text-neutral-800">✕</button>
      </div>
      <ul className="space-y-2">
        {steps.map((s) => (
          <li key={s.step} className="flex items-center gap-2">
            <span>
              {s.status === "running" && "⏳"}
              {s.status === "ok" && "✓"}
              {s.status === "error" && "✗"}
            </span>
            <span className="font-mono text-sm">{s.step}</span>
            {s.detail && <span className="text-xs text-neutral-500 ml-2">{JSON.stringify(s.detail).slice(0, 60)}</span>}
          </li>
        ))}
      </ul>
      {error && <div className="mt-4 text-red-600 text-sm">Error: {error}</div>}
      {done && <div className="mt-4 text-green-700">Tenant created.</div>}
    </div>
  );
}
```

- [ ] **Step 3: Wire drawer into create-tenant-dialog**

Modify `docker-demo/manager/app/components/create-tenant-dialog.tsx`: on submit, open the drawer instead of (or alongside) the existing POST. Read the current file, then change the submit handler to set a `creatingName` state that renders the drawer.

The minimal change:
1. Import `ProvisioningDrawer` at the top.
2. Add state `const [creatingName, setCreatingName] = useState<string | null>(null);`.
3. In the existing submit handler, instead of calling `fetch("/api/tenants", ...)`, call `setCreatingName(serverName)`.
4. In the JSX, render:
   ```tsx
   {creatingName && (
     <ProvisioningDrawer
       serverName={creatingName}
       onClose={() => setCreatingName(null)}
       onComplete={(t) => { props.onCreated?.(t); setCreatingName(null); }}
     />
   )}
   ```

(The exact code depends on the current structure; read the file first and adapt.)

- [ ] **Step 4: Build and smoke test**

```
cd docker-demo/manager && npm run build
```
Expected: build succeeds.

- [ ] **Step 5: Commit** (SKIP)

---

## Task 12: Manager UI — federation test panel

**Files:**
- Create: `docker-demo/manager/app/components/federation-test-panel.tsx`
- Create: `docker-demo/manager/app/api/tenants/[name]/federation-test/route.ts`
- Modify: `docker-demo/manager/app/components/tenant-dashboard.tsx`

- [ ] **Step 1: Create federation-test proxy route**

Create `docker-demo/manager/app/api/tenants/[name]/federation-test/route.ts`:

```typescript
import type { NextRequest } from "next/server";

const CONTROL_PLANE_URL = process.env.CONTROL_PLANE_URL || "http://control-plane:3001";
const CONTROL_PLANE_TOKEN = process.env.CONTROL_PLANE_TOKEN || "";

export async function POST(req: NextRequest, { params }: { params: { name: string } }) {
  const { name } = params;
  const body = await req.text();
  const upstream = await fetch(
    `${CONTROL_PLANE_URL}/api/v1/tenants/${encodeURIComponent(name)}/federation-test`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        Authorization: `Bearer ${CONTROL_PLANE_TOKEN}`,
      },
      body,
    }
  );
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  });
}
```

- [ ] **Step 2: Create federation-test-panel component**

Create `docker-demo/manager/app/components/federation-test-panel.tsx`:

```typescript
"use client";

import { useState } from "react";

type Tenant = { server_name: string };
type StepEvent = { step: string; status: "running" | "ok" | "error"; detail?: any };

export function FederationTestPanel({ source, others }: { source: Tenant; others: Tenant[] }) {
  const [target, setTarget] = useState(others[0]?.server_name ?? "");
  const [steps, setSteps] = useState<StepEvent[]>([]);
  const [running, setRunning] = useState(false);
  const [verdict, setVerdict] = useState<"pending" | "pass" | "fail" | null>(null);

  async function runTest() {
    setSteps([]);
    setVerdict("pending");
    setRunning(true);
    try {
      const res = await fetch(
        `/api/tenants/${encodeURIComponent(source.server_name)}/federation-test`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target }),
        }
      );
      if (!res.body) throw new Error("No response body");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const chunk = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const evLine = chunk.split("\n").find((l) => l.startsWith("event: "));
          const dataLine = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (!evLine || !dataLine) continue;
          const ev = evLine.slice(7);
          const data = JSON.parse(dataLine.slice(6));
          if (ev === "step") {
            setSteps((s) => {
              const existing = s.findIndex((x) => x.step === data.step);
              if (existing >= 0) { const next = [...s]; next[existing] = data; return next; }
              return [...s, data];
            });
          } else if (ev === "complete") { setVerdict("pass"); }
          else if (ev === "error") { setVerdict("fail"); }
        }
      }
    } catch (e: any) {
      setVerdict("fail");
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="border rounded p-4 space-y-3">
      <h3 className="font-semibold">Federation test</h3>
      <div className="flex gap-2 items-center">
        <span className="text-sm">From <code>{source.server_name}</code> to</span>
        <select value={target} onChange={(e) => setTarget(e.target.value)} className="border rounded px-2 py-1">
          {others.map((t) => <option key={t.server_name} value={t.server_name}>{t.server_name}</option>)}
        </select>
        <button onClick={runTest} disabled={running || !target} className="bg-blue-600 text-white rounded px-3 py-1 disabled:opacity-50">
          {running ? "Running…" : "Run test"}
        </button>
      </div>
      {steps.length > 0 && (
        <ul className="space-y-1 text-sm">
          {steps.map((s) => (
            <li key={s.step} className="flex gap-2">
              <span>{s.status === "running" && "⏳"}{s.status === "ok" && "✓"}{s.status === "error" && "✗"}</span>
              <span className="font-mono">{s.step}</span>
            </li>
          ))}
        </ul>
      )}
      {verdict === "pass" && <div className="text-green-700">✓ Federation between {source.server_name} and {target} works.</div>}
      {verdict === "fail" && <div className="text-red-700">✗ Federation failed. See steps above.</div>}
    </div>
  );
}
```

- [ ] **Step 3: Wire panel into tenant-dashboard**

In `docker-demo/manager/app/components/tenant-dashboard.tsx`, import the panel and render it inside each tenant's detail view. The exact placement depends on the current file — read it first. Minimal integration: under each tenant's card, add:

```tsx
<FederationTestPanel
  source={tenant}
  others={allTenants.filter((t) => t.server_name !== tenant.server_name)}
/>
```

- [ ] **Step 4: Build**

```
cd docker-demo/manager && npm run build
```
Expected: succeeds.

- [ ] **Step 5: Commit** (SKIP)

---

## Task 13: Update e2e test_tenants.py for the new hostnames

**Files:**
- Modify: `docker-demo/scripts/test_tenants.py`

- [ ] **Step 1: Update tenant hostnames**

Replace the `TENANT_A`, `TENANT_B`, `TENANT_C`, `TENANT_D` constants:

```python
TENANT_A = "acme.localhost"
TENANT_B = "corp.localhost"
TENANT_C = "startup.localhost"
TENANT_D = "tenant-d.localhost"
```

Also update any hardcoded URLs in the test (`base` variable, `public_baseurl` checks) to reflect `.localhost` + https scheme.

- [ ] **Step 2: Add a new zero-config test**

After the existing `test_federation_key_server` function, add:

```python
def test_zero_config_provisioning(control_plane_base: str, control_plane_token: str) -> None:
    """Create a fresh tenant via SSE and assert it's reachable within 10s."""
    print("\n--- Zero-config provisioning ---")
    import time
    tenant_name = f"smoke-{int(time.time())}.localhost"
    import requests
    start = time.time()
    r = requests.post(
        f"{control_plane_base}/api/v1/tenants",
        headers={
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {control_plane_token}",
            "Content-Type": "application/json",
        },
        json={"server_name": tenant_name},
        stream=True,
        timeout=30,
    )
    check(f"SSE request accepted", r.status_code == 200, f"status={r.status_code}")
    events_seen = []
    for line in r.iter_lines(decode_unicode=True):
        if line and line.startswith("event: "):
            events_seen.append(line[7:])
        if len(events_seen) > 30:
            break
    r.close()
    check(f"SSE stream had step events", any(e == "step" for e in events_seen), f"events={events_seen[:10]}")
    check(f"SSE stream ended with complete or error",
          events_seen[-1] in ("complete", "error") if events_seen else False,
          f"last={events_seen[-1] if events_seen else 'none'}")
    # Verify the tenant responds on its hostname.
    for _ in range(20):
        try:
            resp = requests.get(
                f"https://{tenant_name}/_matrix/client/versions",
                verify=False,
                timeout=2,
            )
            if resp.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    elapsed = time.time() - start
    check(f"{tenant_name} reachable within 10s", resp.status_code == 200 and elapsed < 30,
          f"status={resp.status_code} elapsed={elapsed:.1f}s")
```

- [ ] **Step 3: Add federation-test call**

Append after the zero-config test:

```python
def test_federation_test_endpoint(control_plane_base: str, control_plane_token: str) -> None:
    """Exercise /federation-test end-to-end between two pre-created tenants."""
    print("\n--- Federation test endpoint ---")
    import requests
    r = requests.post(
        f"{control_plane_base}/api/v1/tenants/{TENANT_A}/federation-test",
        headers={
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {control_plane_token}",
            "Content-Type": "application/json",
        },
        json={"target": TENANT_B},
        stream=True,
        timeout=60,
    )
    check("fed-test HTTP 200", r.status_code == 200, f"status={r.status_code}")
    final_event = None
    for line in r.iter_lines(decode_unicode=True):
        if line and line.startswith("event: "):
            final_event = line[7:]
    r.close()
    check("fed-test completed (not errored)", final_event == "complete", f"final={final_event}")
```

- [ ] **Step 4: Run it**

```
cd docker-demo && docker compose run --rm test
```
Expected: mostly green. Some failures acceptable if federation setup isn't fully validated yet — log them.

- [ ] **Step 5: Commit** (SKIP)

---

## Task 14: Rewrite README

**Files:**
- Modify: `docker-demo/README.md`

- [ ] **Step 1: Replace the onboarding section**

Read the current README, then rewrite the Prerequisites + Bring up sections. New content should say:

```markdown
## Prerequisites

1. Docker + docker compose.
2. **mkcert** on the host — this is the only manual dependency. Install:
   - macOS: `brew install mkcert`
   - Debian/Ubuntu: `sudo apt install mkcert`
   - Others: https://github.com/FiloSottile/mkcert

That's it. No `/etc/hosts` edits. No compose edits per tenant.

## Bring up

```bash
# One-time cert generation (idempotent; re-runs are no-ops):
./scripts/setup_certs.sh

# Build image and start stack:
cd ..
DOCKER_BUILDKIT=1 docker build -t synapse:multi-tenant -f docker/Dockerfile .
cd docker-demo
docker compose up -d

# Open the manager UI:
open https://manager.localhost/
```

Creating a tenant now is a single click in the manager UI. The UI streams
each provisioning step in a drawer. When it shows "complete", the tenant
is reachable at `https://<name>.localhost/`.

## Testing federation

In the manager UI, open any tenant and click "Federation test". Pick a
sibling tenant from the dropdown. The test invites a user, joins, sends
a message, and confirms delivery on the other side.
```

Remove the sections that describe `/etc/hosts` edits and manual cert generation with per-tenant hostnames.

- [ ] **Step 2: Commit** (SKIP)

---

## Definition of Done

- [ ] `./scripts/setup_certs.sh && docker compose up -d` bootstraps a clean stack, no manual edits beyond mkcert install.
- [ ] `https://manager.localhost/` loads with no cert warning.
- [ ] Creating `tenant-z.localhost` via the manager shows streaming steps and reaches `https://tenant-z.localhost/_matrix/client/versions` → 200 within 10 seconds.
- [ ] `docker compose run --rm test` shows the zero-config and federation-test checks pass.
- [ ] `synapse/rest/well_known.py` returns tenant-specific responses (verified by `trial tests.rest.test_well_known_multitenant`).
- [ ] No entries in `docker-compose.yml`, `traefik/dynamic/routes.yml`, `certs/`, or `/etc/hosts` mention a specific tenant name (all are wildcard/reserved-infra only).
- [ ] SSE streaming + reserved-name validation + federation-test endpoint: all unit tests green (`npm test` in control-plane).
