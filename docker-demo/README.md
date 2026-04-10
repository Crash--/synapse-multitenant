# 2-Tenant Element Demo

A self-contained, minimalist multi-tenant Synapse rig you can point
Element at. Two tenants, one Synapse process, fronted by Traefik on
the standard HTTP/HTTPS ports.

| Tenant | Homeserver URL | Postgres schema |
|---|---|---|
| **A** | `http://matrix.tenant-a.com` | `tenant_matrix_tenant_a_com` |
| **B** | `http://matrix.tenant-b.com` | `tenant_matrix_tenant_b_com` |

Tenant routing is by `Host` header — Traefik forwards both hostnames
to the same `synapse-demo-server` container, which then resolves the
active tenant from the `Host` and binds the right Postgres schema /
signing key / media root for the rest of the request.

## One-time host setup

Element needs to resolve the two tenant hostnames to your local
machine. Add this to `/etc/hosts`:

```
127.0.0.1 matrix.tenant-a.com matrix.tenant-b.com
```

(On Linux/macOS: `sudo $EDITOR /etc/hosts`. On Windows: edit
`C:\Windows\System32\drivers\etc\hosts` as administrator.)

## Free up port 80

Traefik claims ports 80 and 443. The larger `docker-multitenant/`
dev rig in this repo binds port 80 with its own nginx. Stop it
before bringing the demo up:

```bash
docker stop synapse-mt-nginx
```

## Build the image

From the **repository root** (not `docker-demo/`), build using the
standard Synapse Dockerfile. Since this is a fork, the `synapse/`
directory already contains all multi-tenant patches — no overlay
step needed:

```bash
DOCKER_BUILDKIT=1 docker build -t synapse:multi-tenant -f docker/Dockerfile .
```

Rebuild after pulling new changes.

## Bring it up

From the `docker-demo/` directory:

```bash
docker compose up -d
docker compose logs -f synapse
```

When you see `Synapse now listening on TCP port 8008` *and* the
`copy-schemas` one-shot has exited cleanly, you're ready.

## Connect with Element

1. Open Element (web, desktop, or mobile).
2. **Sign in** → **Edit** the homeserver field.
3. Enter `http://matrix.tenant-a.com` (or `http://matrix.tenant-b.com`).
4. **Create account** — registration is enabled.
5. Pick any username and password. You'll end up with `@you:matrix.tenant-a.com`.

Repeat for the other tenant in a private window or second device to
confirm the two homeservers behave independently.

> Element warns about non-`localhost` `http://` URLs. Click through —
> this is a local demo. To get rid of the warning entirely, follow
> the **Optional: green-padlock HTTPS** section below.

## Optional: green-padlock HTTPS via mkcert

Traefik already listens on port 443. By default it serves a built-in
self-signed certificate, which the browser will warn about once. To
get a locally-trusted certificate (no warning, green padlock), use
[mkcert](https://github.com/FiloSottile/mkcert):

```bash
# 1. Install mkcert and add its root CA to your system trust store
#    (one-time per machine).
mkcert -install

# 2. From the docker-demo/ directory, generate a single cert that
#    covers both tenant hostnames.
mkdir -p certs
mkcert \
    -cert-file certs/tenants.crt \
    -key-file  certs/tenants.key \
    matrix.tenant-a.com matrix.tenant-b.com

# 3. Restart Traefik so it picks up the cert via its file provider.
docker compose restart traefik
```

Then point Element at `https://matrix.tenant-a.com` (note the `s`)
and you'll get the real padlock. The cert is loaded by
`traefik/dynamic/tls.yml`, which Traefik watches at runtime — no
rebuild needed.

If `certs/tenants.crt` is missing Traefik logs a warning at startup
and falls back to its self-signed cert. Plain `http://` always works.

## What to look for

- Users created on `matrix.tenant-a.com` cannot see or message users
  on `matrix.tenant-b.com`. Their accounts live in different Postgres
  schemas — registering the same username (`alice`) on both tenants
  succeeds and produces two completely independent accounts.
- Each tenant has its own signing key under `data/keys/`, served by
  `/_matrix/key/v2/server` against the matching hostname.
- Each tenant has its own media root under `data/media/<tenant>/`.
- Synapse log lines tag every request with the active tenant:

  ```
  [server_name=matrix.tenant-a.com] synapse.access.http.8008 - INFO -
      Processed request: ... POST /_matrix/client/v3/login ...
  ```

## Smoke tests (phases 1–6)

An automated test script exercises every multi-tenant feature from
phases 1 through 6:

```bash
# From the host (after the stack is up and copy-schemas has finished):
python3 scripts/test_tenants.py

# Or from inside the Synapse container:
docker compose exec synapse python3 /scripts/test_tenants.py
```

The script tests:

| Phase | What's tested |
|-------|---------------|
| 1-2 | Tenant routing (`/_matrix/client/versions`), `.well-known/matrix/client`, user registration, cross-tenant isolation |
| 4 | Per-tenant rate-limit config (tenant-a: generous, tenant-b: tight) |
| 5 | Media upload on tenant-a, download, cross-tenant media isolation |
| 4/6 | Admin API `/_synapse/admin/v1/tenants` listing |
| 6 | SIGHUP reload — signal sent, both tenants verified healthy after |

Phase 3 (SSO, email, push) code is overlaid via bind mounts but cannot
be end-to-end tested without external services (SMTP, IdP). The code
paths are exercised — they fall through to global/default config.

## Tear down

```bash
docker compose down
```

The Postgres data lives on tmpfs, so this wipes all accounts. Media
and signing keys persist under `./data/` (delete the folder to start
fresh).

## How the schema cloning works

PostgreSQL schemas in this fork are populated in two passes:

1. On its first boot Synapse runs all of its migrations against the
   `public` schema (because no tenant context is bound at startup
   time), so `public` ends up with the full ~168-table catalogue.
2. The one-shot `copy-schemas` service then runs
   `scripts/copy_tenant_tables.py`, which clones the table layout
   (`CREATE TABLE … LIKE public.<table> INCLUDING ALL`) and a small
   set of singleton seed rows into every `tenant_*` schema.

After that, any request that arrives with a tenant Host header sets
`search_path = tenant_*, public`, so writes land in the tenant's own
tables instead of falling through to `public`. Without this clone
step the tenant schemas would be empty and Postgres would silently
route every read **and** write back to `public`, breaking isolation.

## How it relates to the rest of the repo

The `synapse:multi-tenant` image is built using the standard
`docker/Dockerfile` (same as upstream Synapse). Since this is a fork,
the `synapse/` directory already contains all multi-tenant code — the
build produces a fully patched image with no overlay step. After
editing code, rebuild with
`DOCKER_BUILDKIT=1 docker build -t synapse:multi-tenant -f docker/Dockerfile .`
from the repo root, then `docker compose restart synapse`.

For the full development rig (Prometheus, three tenants, hot iteration
via bind mounts on every patched module) see `../docker-multitenant/`.
