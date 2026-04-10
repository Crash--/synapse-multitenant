# Multi-Tenant Synapse

This document describes the multi-tenant features added to this Synapse fork, allowing a single Synapse process to serve multiple Matrix homeserver domains with isolated data.

## Overview

Traditional Synapse deployments require one process (typically ~150MB RAM) per homeserver domain. This multi-tenant fork allows a single Synapse process to serve multiple domains, with each tenant isolated at the database schema and filesystem level.

### Benefits

- **Reduced resource usage**: ~19MB per tenant vs ~150MB per dedicated instance
- **Simplified deployment**: Single process to manage, monitor, and scale
- **Shared infrastructure**: Single PostgreSQL database, single reverse proxy configuration
- **Complete isolation**: Each tenant has separate database schema, signing keys, and media storage

## Isolation model

Tenant isolation rests on two mechanisms (identifier isolation +
schema-per-tenant). For the full design including the catalogue of
tables and sequences where schema isolation is load-bearing, see
[multi_tenant_isolation_model.md](multi_tenant_isolation_model.md).

### Architecture

```
                    ┌─────────────────┐
                    │  Reverse Proxy  │
                    │   (Traefik)     │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  Synapse Fork   │
                    │  (single proc)  │
                    │                 │
                    │ ┌─────────────┐ │
                    │ │TenantRouter │ │  ← Middleware extracts tenant from Host
                    │ └──────┬──────┘ │
                    │        │        │
                    │ ┌──────▼──────┐ │
                    │ │TenantContext│ │  ← Per-request tenant state
                    │ └──────┬──────┘ │
                    └────────┼────────┘
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
    ┌─────▼─────┐      ┌─────▼─────┐      ┌─────▼─────┐
    │  Schema:  │      │  Schema:  │      │  Schema:  │
    │  acme     │      │  corp     │      │  startup  │
    └───────────┘      └───────────┘      └───────────┘
                    PostgreSQL (shared)
```

## Configuration

### Enabling Multi-Tenant Mode

Add the following to your `homeserver.yaml`:

```yaml
multi_tenant:
  enabled: true
  default_schema: "public"

  tenants:
    - server_name: "acme.com"
      database_schema: "tenant_acme"
      signing_key_path: "/etc/synapse/keys/acme.signing.key"
      media_store_path: "/var/synapse/media/acme"
      registration_enabled: false
      federation_enabled: true

    - server_name: "corp.io"
      database_schema: "tenant_corp"
      signing_key_path: "/etc/synapse/keys/corp.signing.key"
      media_store_path: "/var/synapse/media/corp"
      registration_enabled: true
      federation_enabled: true
```

### Tenant Configuration Options

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `server_name` | Yes | - | Matrix server name (domain) for this tenant |
| `database_schema` | Yes | - | PostgreSQL schema for tenant data |
| `signing_key_path` | Yes | - | Path to the tenant's signing key file |
| `media_store_path` | No | Shared path | Directory for tenant's media files |
| `registration_enabled` | No | `false` | Allow new user registration |
| `federation_enabled` | No | `true` | Allow federation with other servers |
| `max_users` | No | Unlimited | Maximum users for this tenant |
| `max_rooms` | No | Unlimited | Maximum rooms for this tenant |
| `host_aliases` | No | `[]` | Additional hostnames that route to this tenant |

## Setup Guide

### Prerequisites

- PostgreSQL database with support for multiple schemas
- Python 3.11+
- Synapse dependencies installed

### Step 1: Generate Signing Keys

Each tenant needs its own signing key:

```bash
# Using the CLI tool
scripts/synapse_tenant generate-key --output /etc/synapse/keys/acme.signing.key

# Or using synapse directly
python -m synapse.app.homeserver \
    --generate-keys \
    --config-path homeserver.yaml \
    --signing-key-path /etc/synapse/keys/acme.signing.key
```

### Step 2: Create Database Schemas

Each tenant requires its own PostgreSQL schema:

```bash
# Create schema for a single tenant
python -m scripts.create_tenant_schema \
    --config-path homeserver.yaml \
    --tenant-name acme.com

# Or create schemas for all configured tenants
python -m scripts.create_tenant_schema \
    --config-path homeserver.yaml \
    --all-tenants
```

For existing deployments, you can copy the table structure:

```bash
python -m scripts.create_tenant_schema \
    --config-path homeserver.yaml \
    --tenant-name acme.com \
    --from-template \
    --template-schema public
```

### Step 3: Create Media Directories

```bash
mkdir -p /var/synapse/media/acme
mkdir -p /var/synapse/media/corp
chown synapse:synapse /var/synapse/media/*
```

### Step 4: Configure Reverse Proxy

Configure your reverse proxy to route traffic based on the `Host` header:

#### Traefik Example

```yaml
http:
  routers:
    synapse-all-tenants:
      rule: "Host(`acme.com`) || Host(`corp.io`)"
      service: synapse
      entryPoints:
        - websecure
      tls:
        certResolver: letsencrypt

  services:
    synapse:
      loadBalancer:
        servers:
          - url: "http://synapse:8008"
```

#### Nginx Example

```nginx
server {
    listen 443 ssl http2;
    server_name acme.com corp.io;

    location /_matrix {
        proxy_pass http://localhost:8008;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

### Step 5: Start Synapse

```bash
python -m synapse.app.homeserver -c homeserver.yaml
```

## CLI Tool Reference

The `synapse_tenant` CLI tool helps manage tenants:

### List Tenants

```bash
scripts/synapse_tenant list -c homeserver.yaml
```

### Create a New Tenant

```bash
scripts/synapse_tenant create \
    -c homeserver.yaml \
    --server-name newcorp.com \
    --registration-enabled
```

This will:
1. Generate a signing key
2. Create the database schema
3. Create the media directory
4. Output the YAML configuration to add

### Validate Configuration

```bash
scripts/synapse_tenant validate -c homeserver.yaml
```

Checks:
- Required fields present
- Signing keys exist
- Database schemas exist
- Media directories exist

### Initialize Database Schema

```bash
scripts/synapse_tenant init-schema \
    -c homeserver.yaml \
    --server-name acme.com \
    --from-template
```

## Admin API

### List All Tenants

```http
GET /_synapse/admin/v1/tenants
Authorization: Bearer <admin_access_token>
```

Response:
```json
{
    "tenants": [
        {
            "server_name": "acme.com",
            "database_schema": "tenant_acme",
            "registration_enabled": false,
            "federation_enabled": true,
            "media_store_path": "/var/synapse/media/acme"
        }
    ],
    "total": 1,
    "multi_tenant_enabled": true
}
```

### Get Tenant Details

```http
GET /_synapse/admin/v1/tenants/{server_name}
Authorization: Bearer <admin_access_token>
```

### Get Tenant Status

```http
GET /_synapse/admin/v1/tenants/{server_name}/status
Authorization: Bearer <admin_access_token>
```

Response:
```json
{
    "server_name": "acme.com",
    "status": "active",
    "keys_loaded": true,
    "database_schema": "tenant_acme",
    "registration_enabled": false,
    "federation_enabled": true
}
```

### Reload Tenant Keys

After rotating signing keys:

```http
POST /_synapse/admin/v1/tenants/{server_name}/reload_keys
Authorization: Bearer <admin_access_token>
```

## How It Works

### Tenant Detection

When a request arrives, the tenant is determined from:
1. The `Host` HTTP header
2. Matched against configured `server_name` and `host_aliases`

The tenant context is stored using Python's `contextvars`, ensuring isolation even in async code.

### Database Isolation

Each tenant's data is stored in a separate PostgreSQL schema:

```sql
-- Tenant A queries use:
SET search_path TO tenant_acme, public;

-- Tenant B queries use:
SET search_path TO tenant_corp, public;
```

The `search_path` is set at the start of each database transaction based on the current tenant context.

### Signing Keys

Each tenant has its own Ed25519 signing key for:
- Signing events
- Federation authentication
- Key server responses

The `MultiTenantKeyring` class manages keys for all tenants.

### Media Storage

Media files are stored in tenant-specific directories:

```
/var/synapse/media/
├── acme.com/
│   ├── local_content/
│   ├── local_thumbnails/
│   ├── remote_content/
│   └── url_cache/
└── corp.io/
    ├── local_content/
    └── ...
```

## Federation

### Between Local Tenants

When users from different local tenants interact (e.g., `@user:acme.com` joins a room on `corp.io`), the system detects this is a "local federation" scenario and can optimize the interaction.

### With External Servers

Federation with external servers works normally. Each tenant has its own federation identity based on its server name and signing key.

### Key Server

The `/_matrix/key/v2/server` endpoint returns the correct signing key based on the requested server name.

## Troubleshooting

### Tenant Not Found

If requests return "tenant not found":
1. Check the `Host` header matches a configured `server_name`
2. Verify multi-tenant mode is enabled
3. Check for typos in the configuration

### Database Errors

If you see "relation does not exist" errors:
1. Verify the tenant's schema exists: `\dn` in psql
2. Run the schema creation script
3. Check schema permissions

### Signing Key Errors

If signing/verification fails:
1. Check the signing key file exists and is readable
2. Verify the key path in configuration
3. Try reloading keys via Admin API

### Media Not Found

If media uploads/downloads fail:
1. Check media directory exists and has correct permissions
2. Verify the `media_store_path` configuration
3. Check disk space

## Performance Considerations

### Connection Pooling

The connection pool is shared across all tenants. Monitor pool exhaustion under high load.

### Memory Usage

Approximate memory per tenant:
- Base overhead: ~10MB
- Per active user: ~0.5MB
- Per joined room: ~0.2MB

### Rate Limiting

Rate limits are applied per-tenant. Consider adjusting limits for busy tenants.

## Migration from Standalone Synapse

To migrate an existing Synapse deployment to a multi-tenant setup:

1. Create a new schema for the existing data:
   ```bash
   python -m scripts.create_tenant_schema \
       -c homeserver.yaml \
       --tenant-name existing.server \
       --schema-name public \
       --from-template
   ```

2. Configure the tenant pointing to the existing data

3. Gradually migrate to tenant-specific schema if desired

## Security Considerations

### Data Isolation

- Each tenant's data is completely isolated at the database level
- Media files are stored in separate directories
- Signing keys are tenant-specific

### Admin Access

- Server admins have access to all tenants
- Tenant-specific admin roles are not yet implemented
- Consider using separate admin tokens per tenant

### Network Security

- Use TLS for all connections
- Ensure reverse proxy correctly forwards Host headers
- Consider tenant-specific rate limiting

## Limitations

- Hot-adding tenants requires server restart
- Tenant-specific workers are not yet supported
- Cross-tenant admin operations require server admin privileges
- Backup/restore is at the database level, not per-tenant

## Connection pool & capacity

### Search path switching

Each database operation sets the PostgreSQL `search_path` to the active
tenant's schema. Phase 8 optimized this from 3 SQL commands per call
(SHOW + SET + restore) to at most 1 (session-level SET), with a
connection-level cache that skips the SET entirely when the same tenant
reuses the connection.

### Pool configuration

The shared connection pool (`cp_max` in `homeserver.yaml`) is sized to
serve all tenants concurrently. Recommended values:

| Tenants | `cp_max` | Notes |
|---------|----------|-------|
| 1-10 | 10 | Default; direct Postgres connection is fine |
| 10-50 | 50 | Add pgbouncer for connection multiplexing |
| 50-200 | 50-100 | pgbouncer required; raise Postgres `max_connections` |

### Capacity estimates

| Configuration | Tenant ceiling | Limiting factor |
|--------------|----------------|----------------|
| Default (`cp_max=10`, no pgbouncer) | ~10-15 | Pool starvation |
| Pool tuned (`cp_max=50`) | ~30-50 | GIL + SET overhead |
| + schema caching | ~50-100 | GIL + rate limiter memory |
| + pgbouncer (session mode) | ~100-200 | GIL (hard ceiling) |

The GIL ceiling is addressed by worker support (phase 12).

### pgbouncer

The `docker-multitenant/` demo includes a pgbouncer sidecar in session
mode. Session mode pins connections for the client session duration,
which is required because Synapse uses session-level `SET search_path`.
Transaction mode is **not compatible** — it would reset `search_path`
between transactions.

pgbouncer settings in the demo:
- `POOL_MODE=session`
- `DEFAULT_POOL_SIZE=50`
- `MAX_CLIENT_CONN=200`
- `MAX_DB_CONNECTIONS=100`
