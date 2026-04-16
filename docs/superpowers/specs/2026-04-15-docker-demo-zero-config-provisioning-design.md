# Docker Demo — Zero-Config Tenant Provisioning Design

**Date:** 2026-04-15
**Branch:** `feature/multi-tenant`
**Scope:** `docker-demo/` stack only. Sibling `docker-multitenant/` rig is unaffected.
**Related specs:**
- `2026-04-09-dynamic-tenant-control-plane-design.md` — phase 7 DB-driven tenancy
- `2026-04-10-phase-10-federation-inbound-design.md` — phase 10 federation inbound
- Future: LemonLDAP/LDAP integration — separate spec, depends on this one

---

## 1. Problem Statement

Adding a new tenant to the docker-demo stack currently requires **five manual steps**:

1. Edit `docker-compose.yml` to add per-tenant Traefik labels (`Host()` rules).
2. Edit `/etc/hosts` on the host to resolve the new hostname.
3. Regenerate the TLS cert with `mkcert` including the new hostname.
4. Edit `config/homeserver.yaml` to add the tenant block (signing key path, schema, secrets).
5. Restart the Traefik and Synapse containers.

This is tolerable for 3-4 fixed tenants but defeats the purpose of a dynamic multi-tenant demo. It also blocks the planned demo UX ("click Create Tenant → tenant is usable in 10s").

**Goal:** A new tenant is fully provisioned and reachable via HTTPS with valid TLS by a single control-plane API call (or one manager-UI click). No file edits, no restarts, no host-OS interaction after first-time setup.

**Non-goal:** Production-grade cert automation, ACME/Let's Encrypt, federation with the public Matrix network, LemonLDAP/LDAP (all separate work).

## 2. Design Principles

- **One source of truth for tenants:** the `public.tenants` table. Synapse loads from DB at startup and on `POST /_synapse/admin/v1/tenants/reload` (phase 7). The control plane mutates this table. No YAML tenant config involved.
- **Wildcards where they're valid.** A single `*.localhost` TLS cert, a single Traefik `HostRegexp` route, a single dnsmasq wildcard DNS record — all cover current and future tenants with zero per-tenant churn.
- **Federation works between siblings.** Cross-tenant traffic loops through Traefik over HTTPS, not through in-process shortcuts. This mirrors real-world federation and exercises phases 9/10/10' end-to-end.
- **Demo-pragmatic where production demands more.** Cert trust inside the Synapse container uses `federation_verify_certificates: false` rather than installing a custom CA. Documented as a known demo-only shortcut.

## 3. Hostname & DNS

### 3.1 Hostname convention

Tenants use `<tenant>.localhost` (flat 2-level). Examples: `acme.localhost`, `corp.localhost`, `tenant-z.localhost`. User IDs: `@alice:acme.localhost`.

**Rationale:**
- RFC 6761 guarantees `.localhost` resolves to `127.0.0.1` — no DNS infrastructure needed on the host.
- A single wildcard `*.localhost` covers all 2-level subdomains for cert and Traefik regex purposes.
- Matches the sibling `docker-multitenant/` convention.

### 3.2 Reserved infra hostnames

Infra services use their own 2-level `.localhost` names:

- `manager.localhost` — tenant manager UI
- `control-plane.localhost` — control plane REST API
- `traefik.localhost` — Traefik dashboard (optional, disabled by default)

The control plane rejects tenant creation requests whose `server_name` matches any reserved name.

### 3.3 Host-side DNS

Zero configuration. Modern browsers (Chrome, Firefox, Safari) and systemd-resolved resolve `*.localhost → 127.0.0.1` per RFC 6761. Traefik binds `:443` on `127.0.0.1` so `https://acme.localhost/` just works.

### 3.4 Container-side DNS

New compose service: `dnsmasq` (lightweight Alpine-based image). Single config directive:

```
address=/.localhost/<traefik_service_ip>
```

Returns the Traefik container's Docker network IP for any `*.localhost` query. Works for arbitrary 2-level subdomains with no tenant-specific entries.

Containers that need federation-style outbound resolution (`synapse`, plus any others added later) have `dns: [<dnsmasq_ip>]` in their compose spec.

**Stable dnsmasq IP:** dnsmasq must have a predictable address so `synapse` can point at it in `dns:`. Approach: custom bridge network with explicit `ipv4_address` for both `dnsmasq` and `traefik` (e.g. `172.28.0.2` and `172.28.0.3`). The plan will define exact addresses; no tenant-specific config changes this.

Adding a new tenant does not touch dnsmasq config.

## 4. TLS

### 4.1 Certificate generation

Bootstrap runs **once per environment** (idempotent):

```bash
mkcert -install                                  # installs mkcert root CA into host trust store
mkcert -cert-file certs/wildcard.crt \
       -key-file  certs/wildcard.key \
       "*.localhost" localhost
```

- Single cert covers all current and future tenants.
- `mkcert -install` ensures the root CA is trusted by your OS so browsers don't warn.
- Compose has a one-shot `certs` service that runs the script before Traefik starts. If `certs/wildcard.crt` exists and is valid for `*.localhost`, the script is a no-op.

### 4.2 Host dependency

`mkcert` must be installed on the host. The bootstrap script detects this and exits with a clear hint:

> `mkcert not found. Install it: "brew install mkcert" (macOS), "apt install mkcert" (Debian/Ubuntu), or see https://github.com/FiloSottile/mkcert`

This is the only host-side dependency in the design.

### 4.3 Traefik cert loading

`traefik/dynamic/tls.yml`:

```yaml
tls:
  certificates:
    - certFile: /certs/wildcard.crt
      keyFile:  /certs/wildcard.key
```

The `certs/` directory is mounted read-only into the Traefik container.

### 4.4 Cert trust inside the Synapse container

Demo shortcut: `federation_verify_certificates: false` in the base `homeserver.yaml`. Noted as a demo-only decision.

**What would change for production-like trust:** copy `~/.local/share/mkcert/rootCA.pem` into the Synapse image's `/usr/local/share/ca-certificates/` and `update-ca-certificates` at build. Out of scope here.

## 5. Traefik

### 5.1 Provider configuration

Traefik loses its Docker-label provider and gains a file provider watching `traefik/dynamic/`. Static config in `traefik.yml`:

```yaml
providers:
  file:
    directory: /etc/traefik/dynamic
    watch: true

entryPoints:
  websecure:
    address: ":443"
```

### 5.2 Dynamic routes (committed, static, no auto-generation)

`traefik/dynamic/routes.yml`:

```yaml
http:
  routers:
    synapse-wildcard:
      rule: "HostRegexp(`^[a-z0-9-]+\\.localhost$`) && !Host(`manager.localhost`) && !Host(`control-plane.localhost`) && !Host(`traefik.localhost`)"
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
    synapse:       { loadBalancer: { servers: [{ url: "http://synapse:8008" }] } }
    manager:       { loadBalancer: { servers: [{ url: "http://manager:3000" }] } }
    control-plane: { loadBalancer: { servers: [{ url: "http://control-plane:3001" }] } }
```

- Priorities ensure infra hostnames match before the wildcard.
- Adding tenant-z does not touch this file.

### 5.3 Federation port

Traefik binds only `:443`. Remote servers (including siblings) discover the port via `.well-known/matrix/server` (Section 6). No `:8448` binding needed.

### 5.4 Federation self-send loopback

From inside the Synapse container, `tenant-b.localhost` resolves via dnsmasq to Traefik's service IP. Traefik's wildcard route sends the request back to Synapse, which (via phase-10a Authenticator) reads the X-Matrix `destination` header and sets the tenant-b ContextVar before the servlet runs. No special-casing.

## 6. Synapse changes

### 6.1 `.well-known/matrix/server` made tenant-aware

**File:** `synapse/rest/well_known.py::ServerWellKnownResource`.

**Today:**
- `__init__` reads `hs.config.server.server_name` once and caches a single JSON response in `self._response`.
- All tenant hosts get the same response (the global hostname).

**Change:**
- Drop the pre-cached `self._response`.
- On each `render_GET`, read `get_current_tenant()` (set by the Host-header middleware from phase 10'-a).
- Return `{"m.server": f"{tenant.server_name}:443"}` when a tenant context is set, else fall back to the global `server_name:443`.

**Why 443:** Traefik's only TLS entry point. Could be made configurable later, not for this spec.

This closes the phase-10 "deferred to reverse proxy" gap — appropriate now because we control the reverse proxy and handling it in Synapse is cleaner than synthesizing a per-tenant static response in Traefik.

### 6.2 Homeserver config

`config/homeserver.yaml` is pruned of all hardcoded tenant blocks. Tenants are loaded from `public.tenants` at startup and on reload (phase 7 — already implemented).

Adds:

```yaml
multi_tenant:
  enabled: true
  source: "database"

federation_verify_certificates: false   # demo only — see spec §4.4
```

### 6.3 Compose service definition

The `synapse` service no longer has per-tenant Traefik labels. It exposes only port `8008` on the internal Docker network (Traefik does TLS termination). `dns: [<dnsmasq_ip>]` added so outbound federation resolves sibling hostnames.

## 7. Control plane changes

### 7.1 Streaming tenant creation

`POST /api/v1/tenants` gains SSE support:

- **Default** (`Accept: application/json`): unchanged. Single JSON blob with final result. Preserves existing programmatic callers and tests.
- **Streaming** (`Accept: text/event-stream`): response is an SSE stream. One event per provisioning step:

```
event: step
data: {"step": "keygen", "status": "running"}

event: step
data: {"step": "keygen", "status": "ok", "detail": {"key_id": "ed25519:abc"}}

event: step
data: {"step": "schema_clone", "status": "running"}

... etc ...

event: complete
data: {"tenant": {"id": "...", "server_name": "tenant-z.localhost", "status": "active", ...}}
```

Steps (in order): `keygen`, `db_row`, `schema_clone`, `media_dir`, `activate`, `synapse_reload`, `complete`.

On error, the final event is `event: error` with `{step, detail}`.

### 7.2 Federation test endpoint

**New:** `POST /api/v1/tenants/:source/federation-test` — orchestrates a cross-tenant smoke test.

Request body:

```json
{ "target": "tenant-b.localhost" }
```

Steps (streamed as SSE, same format as 7.1):
1. `register_users` — create test user on source tenant + test user on target tenant (admin API).
2. `create_room` — source tenant user creates a room.
3. `invite` — source tenant invites target tenant user.
4. `join` — target tenant user joins.
5. `send_message` — source sends "ping" event.
6. `receive_message` — target polls sync and confirms the event arrived.
7. `cleanup` — leave rooms, deactivate test users (best-effort).
8. `complete` — summary with per-step latency.

Test users are ephemeral and deterministic (`@_fed_test_<timestamp>:<tenant>`). The endpoint is demo-only (auth is the existing bearer token).

### 7.3 Reserved-name validation

Before inserting a tenant row, control plane rejects `server_name ∈ {manager.localhost, control-plane.localhost, traefik.localhost, localhost}`.

## 8. Manager UI changes

### 8.1 Streaming provisioning drawer

Today: "Create tenant" form POSTs JSON, gets a flat result card.

New: on submit, the UI opens a side drawer and opens an SSE stream to the control plane. The drawer renders a checklist: each step shows a spinner while `running`, a ✓ on `ok`, ✗ on `error`. On `complete`, drawer shows the tenant row and a "Go to tenant" link. On error, drawer shows the failing step and detail without blocking retry.

Frontend: no new dependencies — native `EventSource` API handles SSE.

### 8.2 Federation test panel

Each tenant's detail view gets a "Federation test" card:

- Dropdown: "Test federation to..." listing all other active tenants.
- Button: "Run test" → opens a drawer identical in style to the provisioning drawer, streaming the federation-test SSE.
- Shows per-step latency and an overall pass/fail verdict.
- On success: "✓ Federation between `acme.localhost` and `corp.localhost` is working end-to-end."

This is the feature that demonstrates phases 9 + 10 + 10' to a non-technical viewer in a single click.

## 9. Data flow — new tenant end-to-end

```
User clicks "Create tenant-z" in manager UI
        │
        ▼
Manager → POST https://control-plane.localhost/api/v1/tenants  (Accept: text/event-stream)
        │     { "server_name": "tenant-z.localhost" }
        │
        ├── SSE step: keygen         → Ed25519 key, encrypted, stored in public.tenants
        ├── SSE step: db_row         → row inserted, status=provisioning
        ├── SSE step: schema_clone   → tenant_z schema cloned from public
        ├── SSE step: media_dir      → /media/tenant-z created
        ├── SSE step: activate       → status=active
        └── SSE step: synapse_reload → POST /_synapse/admin/v1/tenants/reload
        │
        ▼
Synapse TenantRegistry.reload() picks up tenant-z from DB.

Browser test:
  https://tenant-z.localhost/_matrix/client/versions
  → .localhost resolves to 127.0.0.1 (RFC 6761)
  → Traefik wildcard router → Synapse → Host-header middleware sets tenant-z
  → 200 OK with tenant-z's version info

Federation test (source=acme, target=tenant-z):
  POST /api/v1/tenants/acme.localhost/federation-test { target: "tenant-z.localhost" }
  → control plane registers ephemeral users on both tenants
  → sends invite via client API on acme
  → acme's outbound federation queues to tenant-z.localhost
  → DNS: dnsmasq answers tenant-z.localhost → traefik IP
  → HTTPS to Traefik:443 → wildcard route → Synapse inbound
  → Authenticator reads X-Matrix destination=tenant-z.localhost → tenant ctx set
  → invite delivered to tenant_z schema
  → target user joins, sync confirms → test passes
```

## 10. Testing strategy

### 10.1 New unit tests

- `tests/tenant/test_well_known.py` — new file. Probes:
  - With tenant ContextVar set, response contains `m.server: <tenant>.localhost:443`.
  - With no tenant ContextVar, response falls back to global hostname.
- Control plane test additions:
  - SSE streaming order and content.
  - Reserved-name rejection.
  - Federation-test endpoint happy path (mocked Synapse admin API).

### 10.2 End-to-end smoke test

`docker-demo/scripts/test_tenants.py` gets two new tests:

- **Zero-config provisioning:** create `tenant-smoke.localhost` via SSE, assert all expected events arrive in order, assert HTTPS `GET /_matrix/client/versions` on the new hostname returns 200 within 10 seconds of `complete`.
- **Cross-tenant federation via new endpoint:** call `/federation-test` between two freshly-created tenants, assert all steps succeed.

### 10.3 Manual verification

- `docker compose down -v && docker compose up` → bootstrap completes with no manual steps beyond `mkcert` install (pre-req).
- Browser: `https://acme.localhost` → 200, valid TLS, no cert warning.
- Element Web: connect as `@alice:acme.localhost` → login works.
- Manager: create tenant-z → drawer shows streaming steps → tenant-z reachable within 10s.
- Federation test between acme and tenant-z → all steps green.

## 11. Compose topology after changes

```
services:
  dnsmasq        # NEW — wildcard .localhost → traefik
  certs          # NEW one-shot — generates wildcard cert via mkcert
  traefik        # changed — file provider, single wildcard route, no labels
  postgres       # unchanged
  pgbouncer      # unchanged
  init-schemas   # unchanged (seed public schema)
  copy-schemas   # unchanged (one-shot, clones from public)
  synapse        # changed — no per-tenant labels, dns points at dnsmasq,
                 # federation_verify_certificates: false, tenants source=database
  control-plane  # changed — SSE on POST /tenants, /federation-test endpoint
  manager        # changed — streaming drawer, federation test panel
```

Removed: `keygen` one-shot (control plane generates keys on-demand per tenant).

Removed from compose: all hardcoded per-tenant env vars, `TENANTS` variables, per-tenant Traefik labels.

## 12. Definition of Done

- [ ] `docker compose down -v && docker compose up` bootstraps with no manual file edits (mkcert install is pre-req, documented in README).
- [ ] `https://acme.localhost/_matrix/client/versions` returns 200 with trusted TLS on a clean checkout.
- [ ] Manager "Create tenant" runs end-to-end via streaming drawer; tenant reachable within 10 seconds.
- [ ] Federation-test button passes between any two active tenants.
- [ ] No tenant-specific entries in `docker-compose.yml`, `traefik/dynamic/`, `/etc/hosts`, or the cert SAN list.
- [ ] `ServerWellKnownResource` returns per-tenant responses (verified by unit test + curl against each tenant's well-known URL).
- [ ] Unit tests green, SSE provisioning smoke test green, federation-test smoke test green.
- [ ] `docker-demo/README.md` rewritten to reflect the new flow; zero mentions of `/etc/hosts` edits or compose-file edits.

## 13. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| `.localhost` doesn't resolve on some host OS configurations | User can't reach the demo in the browser | Pre-flight check in bootstrap script: `curl -v https://localhost` + explicit note in README about OS requirements |
| dnsmasq container's DNS port (53/udp) conflicts with host's local DNS | Container startup fails | Bind dnsmasq only on the internal Docker network, not on host ports |
| `federation_verify_certificates: false` accidentally leaks into prod configs | Security weakness | Comment in the YAML file explicitly warns this is demo-only; separate prod config template elsewhere in docs |
| SSE consumer on manager UI drops connection mid-stream | User sees incomplete progress | Client reconnects and polls `GET /tenants/:name` for final state; control plane operations are idempotent per step |
| Matrix clients cache well-known responses for hours | Tenant-aware well-known change not observed during demo | Use short `"m.server"` response TTL; note in README |
| mkcert root CA trust doesn't survive OS re-image | Cert warnings return | `mkcert -install` is idempotent; bootstrap script re-runs on `docker compose up` |

## 14. Migration / rollout

One-shot replacement of the current docker-demo. Old tenants (`matrix.tenant-a.com` style) can't carry forward — their `server_name`s are baked into every event they've persisted. For a demo, this is acceptable: `docker compose down -v` discards state, new tenants use new hostnames.

No code paths in `synapse/` change semantics for tenants whose `server_name` happens to be `.localhost` vs `.com` — the hostname is a free-form string as far as Synapse is concerned.

The sibling `docker-multitenant/` rig is untouched; it continues with its current fixed 3 tenants.

## 15. Out of scope

- LemonLDAP, LDAP, SSO (separate spec).
- Federation to the public Matrix network.
- ACME / Let's Encrypt.
- Installing mkcert root CA inside the Synapse container.
- Worker/replication support for streaming provisioning.
- Tenant deletion via SSE (stays non-streaming; short operation).
- Test-user lifecycle beyond ephemeral creation in federation-test.
