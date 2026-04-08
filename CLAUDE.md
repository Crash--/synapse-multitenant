# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this fork is

This is a **multi-tenant fork of Synapse** (Matrix homeserver) maintained by Linagora on the `feature/multi-tenant` branch. The goal is to let a single Synapse process serve many Matrix homeserver domains, with database-schema-level isolation, instead of running one ~150MB process per domain. All multi-tenant work lives alongside the upstream code; do not assume upstream Synapse documentation reflects this fork's behavior.

Authoritative design doc: `docs/multi_tenant.md`. Read it before making non-trivial changes to tenant routing, key handling, or storage.

## Multi-tenant architecture

Tenant identity flows through the request lifecycle like this:

1. **Routing** — A reverse proxy (nginx/Traefik) forwards the original `Host` header. Synapse matches it against `server_name` / `host_aliases` from the `multi_tenant.tenants` block in `homeserver.yaml`.
2. **Context** — `synapse/tenant_context.py` stores the active `TenantConfig` in a `contextvars.ContextVar`. This is the single source of truth for "which tenant is this request for?" and is async/Twisted-safe. Anywhere in the codebase you can call `get_current_tenant()` instead of plumbing tenant info through call sites.
3. **Registry** — `synapse/tenant_registry.py` loads/holds all configured tenants. `synapse/config/tenants.py` defines the `TenantConfig` dataclass and config parsing.
4. **Database isolation** — Each tenant gets its own PostgreSQL schema. At transaction start, `search_path` is set to `<tenant_schema>, public` so existing Synapse SQL is unchanged. The connection pool is shared.
5. **Signing keys** — `synapse/crypto/multitenant_keyring.py` (`MultiTenantKeyring`) holds per-tenant Ed25519 keys; the `/_matrix/key/v2/server` endpoint returns the key for the requested server name.
6. **Media** — `synapse/media/multitenant_filepath.py` rewrites media paths under a tenant-specific `media_store_path`.
7. **Admin API** — `synapse/rest/admin/tenants.py` exposes `/_synapse/admin/v1/tenants[...]` for listing tenants, status, and reloading keys.

When touching any of the files above, keep in mind: tenant isolation is a **security boundary**. Code that reads/writes data must go through the current tenant context — never hardcode a schema or key path, and never leak data across `TenantConfig` boundaries.

Known limitations (don't "fix" by accident): tenants cannot be hot-added (restart required), there are no per-tenant workers, and backup/restore is database-wide.

## Common commands

### Tests

Synapse uses Twisted Trial. From the repo root:

```bash
# Run all tests
trial tests

# Run a single module / class / test
trial tests.tenant.test_registry
trial tests.tenant.test_context.TenantContextTestCase
trial tests.tenant.test_context.TenantContextTestCase.test_set_and_get

# Run via tox (matches CI environment)
tox -e py
```

Multi-tenant unit tests live in `tests/tenant/` (`test_context.py`, `test_registry.py`, `test_media_filepath.py`).

### Running without compiled Rust (development only)

This fork supports skipping the Rust extension freshness check so you can iterate on Python without rebuilding `synapse_rust`:

```bash
export SYNAPSE_SKIP_RUST_CHECK=1
```

See `synapse/util/rust.py` — the bypass only skips the *staleness check*; the compiled `synapse.synapse_rust` module must still be importable. If you actually modified Rust under `rust/`, rebuild with `poetry install` and unset the variable.

### Tenant CLI

`scripts/synapse_tenant` is the operator tool for managing tenants in a config:

```bash
scripts/synapse_tenant list      -c homeserver.yaml
scripts/synapse_tenant validate  -c homeserver.yaml
scripts/synapse_tenant create    -c homeserver.yaml --server-name newcorp.com --registration-enabled
scripts/synapse_tenant init-schema -c homeserver.yaml --server-name acme.com --from-template
scripts/synapse_tenant generate-key --output /etc/synapse/keys/acme.signing.key
```

Schema bootstrap can also be invoked directly: `python -m scripts.create_tenant_schema -c homeserver.yaml --all-tenants`.

### Local Docker demo

`docker-multitenant/` is a self-contained 3-tenant playground (acme/corp/startup `.localhost`) with PostgreSQL + nginx:

```bash
cd docker-multitenant
docker-compose up -d
docker-compose logs -f synapse
docker-compose run test          # end-to-end tests against the running stack
docker-compose down -v           # tear down + remove volumes

# Smoke a tenant by Host header
curl -H "Host: acme.localhost" http://localhost/_matrix/client/versions
```

Helper scripts under `docker-multitenant/scripts/` (`init_schemas.py`, `generate_keys.py`, `copy_tenant_tables.py`, `test_tenants.py`, `run_server.py`) are demo-specific — not shipped as part of the package.

There is also a `helm-tenant-deploy` skill available for testing Helm-based tenant deployments in a local k3d cluster.

### Lint / typecheck

Standard Synapse tooling applies (`scripts-dev/lint.sh`, `mypy` per `mypy.ini`). Nothing fork-specific.

## Where to look

| If you're changing... | Start here |
|---|---|
| Tenant config schema / parsing | `synapse/config/tenants.py` |
| Per-request tenant state | `synapse/tenant_context.py` |
| Loading / lookup of tenants | `synapse/tenant_registry.py` |
| Signing keys per tenant | `synapse/crypto/multitenant_keyring.py` |
| Media path resolution | `synapse/media/multitenant_filepath.py` |
| Admin endpoints for tenants | `synapse/rest/admin/tenants.py` |
| Operator workflows | `scripts/synapse_tenant`, `scripts/create_tenant_schema.py` |
| Local repro environment | `docker-multitenant/` |
| Design / behavior reference | `docs/multi_tenant.md` |
