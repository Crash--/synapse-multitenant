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
