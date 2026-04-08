# Multi-Tenant Synapse Docker Demo

This Docker setup demonstrates a multi-tenant Synapse deployment with:

- **3 tenants**: acme.localhost, corp.localhost, startup.localhost
- **PostgreSQL** with separate schemas per tenant
- **Nginx** reverse proxy routing by Host header
- **Isolated signing keys** per tenant

## Quick Start

```bash
# Start everything
docker-compose up -d

# Watch logs
docker-compose logs -f synapse

# Run tests
docker-compose run test
```

## Fast iteration loop

The patched Python files under `../synapse/` are bind-mounted into
the running container, so the inner loop is:

```bash
# Edit a file under ../synapse/, then:
docker-compose restart synapse
docker-compose run test
```

No image rebuild required. If you add a *new* patched file, add it
to BOTH `Dockerfile.real` (so a clean build still works) and the
volume list in `docker-compose.yml` (so the hot loop picks it up).

## Observability

`enable_metrics` is on, and a `prometheus` service scrapes Synapse
on every boot:

- Prometheus UI:    <http://localhost:9090>
- Raw scrape:       `curl http://localhost:8008/_synapse/metrics`
- Synapse log file: `./data/logs/synapse.log` (also visible via
  `docker-compose logs synapse`)

The test suite includes three observability checks under
`OBSERVABILITY TESTS`:

1. `/_synapse/metrics` reachable — should pass on a fresh boot.
2. Metrics carry a `tenant=` label — **expected to fail** until the
   audit/instrumentation phase wires the active `TenantConfig` into
   the Prometheus label set.
3. `synapse.log` includes tenant context on request lines — **expected
   to fail** until the same phase threads the `ContextVar` into the
   logging context.

The two failing tests are intentional — they gate the next phase of
the multi-tenancy roadmap (`docs/multi_tenant_roadmap.md`).

## Test Manually

```bash
# Test tenant: acme.localhost
curl -H "Host: acme.localhost" http://localhost/_matrix/client/versions | jq .

# Test tenant: corp.localhost
curl -H "Host: corp.localhost" http://localhost/_matrix/client/versions | jq .

# Test tenant: startup.localhost
curl -H "Host: startup.localhost" http://localhost/_matrix/client/versions | jq .

# Test unknown tenant (should fail)
curl -H "Host: unknown.localhost" http://localhost/_matrix/client/versions | jq .

# Get tenant info
curl -H "Host: acme.localhost" http://localhost/_synapse/admin/v1/tenant | jq .
```

## Architecture

```
                    ┌─────────────────┐
                    │     Nginx       │  Port 80
                    │  (Host Router)  │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │     Synapse     │  Port 8008
                    │  (Multi-Tenant) │
                    │                 │
                    │ ┌─────────────┐ │
                    │ │   Tenant    │ │
                    │ │   Router    │ │
                    │ └──────┬──────┘ │
                    └────────┼────────┘
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
    ┌─────▼─────┐      ┌─────▼─────┐      ┌─────▼─────┐
    │  Schema:  │      │  Schema:  │      │  Schema:  │
    │tenant_acme│      │tenant_corp│      │tenant_    │
    │           │      │           │      │startup    │
    └───────────┘      └───────────┘      └───────────┘
              PostgreSQL (synapse_multitenant)
```

## Tenants

| Tenant | Schema | Registration | Federation |
|--------|--------|--------------|------------|
| acme.localhost | tenant_acme | ✓ Enabled | ✓ Enabled |
| corp.localhost | tenant_corp | ✗ Disabled | ✓ Enabled |
| startup.localhost | tenant_startup | ✓ Enabled | ✗ Disabled |

## Files

- `docker-compose.yml` - Container orchestration
- `Dockerfile` - Synapse image with multi-tenant code
- `nginx/nginx.conf` - Reverse proxy configuration
- `config/homeserver.yaml` - Multi-tenant Synapse config
- `scripts/init-db.sql` - PostgreSQL schema setup
- `scripts/generate_keys.py` - Signing key generator
- `scripts/run_server.py` - Demo server implementation
- `scripts/test_tenants.py` - End-to-end tests

## Cleanup

```bash
docker-compose down -v  # Stop and remove volumes
```
