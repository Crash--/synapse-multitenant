# Multi-Tenant Synapse Demo

A self-contained multi-tenant Synapse rig with a manager UI you can use to
create, inspect, and test tenants. A single Synapse process serves every
tenant; Traefik routes by `Host` header. Tenants are managed by a control
plane and stored in Postgres — no YAML per tenant.

Tenant routing is by `Host` header — Traefik forwards all `*.localhost`
traffic to the same `synapse-demo-server` container, which then resolves the
active tenant from the `Host` and binds the right Postgres schema /
signing key / media root for the rest of the request.

## Prerequisites

1. Docker + docker compose.
2. **mkcert** on the host — this is the only manual dependency:
   - macOS: `brew install mkcert`
   - Debian/Ubuntu: `sudo apt install mkcert`
   - Others: https://github.com/FiloSottile/mkcert

No `/etc/hosts` edits. No compose edits per tenant. No manual cert regeneration.

## Bring up

```bash
# One-time cert generation (idempotent):
./scripts/setup_certs.sh

# Build the Synapse image (from repo root):
cd ..
DOCKER_BUILDKIT=1 docker build -t synapse:multi-tenant -f docker/Dockerfile .
cd docker-demo

# Start the stack:
docker compose up -d

# Open the manager UI:
open https://manager.localhost/      # macOS
# or just visit https://manager.localhost/ in your browser
```

Creating a tenant is a single click in the manager. The UI streams each
provisioning step in a side drawer. When complete, the tenant is
reachable at `https://<name>.localhost/` — no further setup.

## Testing federation between tenants

In the manager UI, open any tenant and click "Run test" in the Federation
test card. Pick a sibling tenant from the dropdown. The test registers
test users on both tenants, creates a room, invites across the boundary,
and confirms message delivery end-to-end. This exercises phases 9, 10,
and 10' in a single click.

## Testing SSO login

Every new tenant gets three demo users seeded in LDAP:

| Email                        | Password |
|------------------------------|----------|
| `alice@<tenant>.localhost`   | `demo`   |
| `bob@<tenant>.localhost`     | `demo`   |
| `charlie@<tenant>.localhost` | `demo`   |

To sign in:

1. Visit `https://<tenant>.localhost/` in a Matrix client (Element, etc.).
2. Start a login; pick "Sign in with LemonLDAP SSO".
3. You're redirected to `https://lemonldap.localhost/` — enter one of the emails above + `demo`.
4. You land back in the client as `@alice:<tenant>.localhost`.

### How it works

- **LemonLDAP** at `https://lemonldap.localhost/` authenticates against **OpenLDAP** at `ldap://openldap:389`.
- **OpenLDAP** is seeded with a per-tenant branch: `o=<tenant>,ou=organizations,dc=demo,dc=local`.
- The **OIDC proxy** at `https://oidc-proxy.localhost/` sits between Synapse and LemonLDAP so that all tenants share a single OIDC client (`synapse-demo`). The proxy validates email-domain == tenant at token exchange to block cross-tenant logins.

### Reset to clean state

```bash
docker compose down -v
./scripts/setup_certs.sh
docker compose up -d
```

## Architecture at a glance

- **Hostnames:** `<tenant>.localhost` — RFC 6761 means browsers auto-resolve
  to 127.0.0.1. No DNS config on the host.
- **Container DNS:** bundled `dnsmasq` answers `*.localhost → traefik` so
  sibling tenants can federate with each other over HTTPS loopback.
- **TLS:** single `mkcert`-generated wildcard cert for `*.localhost`.
- **Traefik:** single `HostRegexp` route — no per-tenant config.
- **Tenants:** loaded from the `public.tenants` Postgres table, managed
  by the control plane. Zero YAML tenant config.

## What to look for

- Users created on one tenant cannot see or message users on another.
  Their accounts live in different Postgres schemas — registering the
  same username (`alice`) on both tenants succeeds and produces two
  completely independent accounts.
- Each tenant has its own signing key, served by
  `/_matrix/key/v2/server` against the matching hostname.
- Each tenant has its own media root.
- Synapse log lines tag every request with the active tenant:

  ```
  [server_name=acme.localhost] synapse.access.http.8008 - INFO -
      Processed request: ... POST /_matrix/client/v3/login ...
  ```

## Smoke tests

An automated test script exercises every multi-tenant feature:

```bash
# From inside the Synapse container:
docker compose exec synapse python3 /scripts/test_tenants.py

# Or from the host (requires requests):
python3 scripts/test_tenants.py [--host localhost]
```

The script tests:

| Phase | What's tested |
|-------|---------------|
| 1-2 | Tenant routing (`/_matrix/client/versions`), `.well-known/matrix/client`, user registration, cross-tenant isolation |
| 4 | Per-tenant rate-limit config |
| 5 | Media upload on tenant-a, download, cross-tenant media isolation |
| 4/6 | Admin API `/_synapse/admin/v1/tenants` listing |
| 6 | SIGHUP reload — signal sent, both tenants verified healthy after |
| 10b | Per-tenant federation key server — distinct keys per tenant |
| 10 | Federation version endpoint, cross-tenant room operations |
| 10' | Zero-config provisioning via SSE, federation-test endpoint |

## Teardown

```bash
docker compose down -v       # removes volumes; state is tmpfs anyway
```

## How the schema cloning works

PostgreSQL schemas in this fork are populated in two passes:

1. On its first boot Synapse runs all of its migrations against the
   `public` schema (because no tenant context is bound at startup
   time), so `public` ends up with the full ~168-table catalogue.
2. When a tenant is provisioned via the control plane, the schema
   initialisation step clones the table layout
   (`CREATE TABLE … LIKE public.<table> INCLUDING ALL`) and a small
   set of singleton seed rows into the new `tenant_*` schema.

After that, any request that arrives with a tenant Host header sets
`search_path = tenant_*, public`, so writes land in the tenant's own
tables instead of falling through to `public`.

## How it relates to the rest of the repo

The `synapse:multi-tenant` image is built using the standard
`docker/Dockerfile` (same as upstream Synapse). Since this is a fork,
the `synapse/` directory already contains all multi-tenant code — the
build produces a fully patched image with no overlay step. After
editing code, rebuild with
`DOCKER_BUILDKIT=1 docker build -t synapse:multi-tenant -f docker/Dockerfile .`
from the repo root, then `docker compose restart synapse`.

For the full development rig (Prometheus, hot iteration via bind mounts
on every patched module) see `../docker-multitenant/`.

For design and architecture details see `../docs/multi_tenant.md`.
