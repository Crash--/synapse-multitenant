# Dynamic Tenant Control Plane — Design Spec

## Context

Phases 1-6 built multi-tenant isolation (schema-per-tenant, per-tenant SSO/email/push/rate-limits, hot-reload via SIGHUP, backup/restore). But the system can't operate as B2B SaaS because:

- Tenants are defined in `homeserver.yaml` — adding one requires config file mutation + restart/SIGHUP.
- No REST API for full tenant lifecycle (create/update/delete).
- The admin plane lives inside Synapse — tenants could theoretically create sub-tenants.
- Synapse requires at least 1 tenant at boot (can't start empty).
- Signing keys are filesystem files — requires shared volumes or manual provisioning.

This spec designs the move to **database-driven dynamic tenant management** via a standalone control plane service.

## Architecture

### Two services, one database

```
                    ┌──────────────────┐
                    │   Control Plane   │  Fastify + Drizzle + TypeScript
                    │  (tenant CRUD)    │
                    └────────┬─────────┘
                             │
              writes         │  HTTP push reload
              ┌──────────────┼──────────────┐
              ▼              ▼              │
     ┌────────────┐  ┌─────────────┐       │
     │  PostgreSQL │  │   Synapse   │◄──────┘
     │  public.    │  │  (reads DB  │
     │  tenants    │  │   at boot   │
     └────────────┘  │   + reload)  │
                     └─────────────┘
```

- **Control Plane** (`docker-demo/control-plane/`) — owns tenant lifecycle. The only writer to `public.tenants`.
- **Synapse** — reads `public.tenants` at startup and on reload. `homeserver.yaml` keeps only global settings + `multi_tenant: { enabled: true, source: "database" }`.
- **Communication** — control plane pushes `POST /_synapse/admin/v1/tenants/reload` (bearer token). Synapse never calls the control plane.
- **Zero-tenant boot** — Synapse starts with empty registry. Control plane adds tenants on demand.

### Tenant status lifecycle

```
provisioning → active ⇄ suspended → deleting
```

## Database Schema

`public.tenants` table — typed columns for core fields, JSONB for complex sub-configs:

```sql
CREATE TABLE public.tenants (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    server_name                 TEXT NOT NULL UNIQUE,
    database_schema             TEXT NOT NULL UNIQUE,
    status                      TEXT NOT NULL DEFAULT 'provisioning'
                                CHECK (status IN ('provisioning','active','suspended','deleting')),

    -- Signing key (AES-256-GCM encrypted)
    signing_key_encrypted       BYTEA NOT NULL,
    signing_key_id              TEXT NOT NULL,

    -- Secrets
    macaroon_secret_key         TEXT,
    form_secret                 TEXT,
    registration_shared_secret  TEXT,

    -- Core config
    media_store_path            TEXT NOT NULL,
    registration_enabled        BOOLEAN NOT NULL DEFAULT false,
    enable_federation           BOOLEAN NOT NULL DEFAULT true,
    max_mau_value               INTEGER NOT NULL DEFAULT 0,
    public_baseurl              TEXT,
    server_notices_mxid         TEXT,
    trusted_key_servers         JSONB DEFAULT '[]',

    -- Complex sub-configs (JSONB — parsed by TenantConfig.from_dict() as-is)
    email_config                JSONB,
    oidc_config                 JSONB,
    cas_config                  JSONB,
    saml_config                 JSONB,
    push_config                 JSONB,
    ratelimit_config            JSONB,
    app_service_config_files    JSONB,

    -- Audit
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    config_version              INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_tenants_status ON public.tenants(status);
```

**Rationale:** Core fields are queryable typed columns. Complex nested configs (OIDC, SAML, rate limits) are JSONB because `TenantConfig.from_dict()` already expects dicts. At B2B scale (hundreds of tenants), JSONB perf is fine.

## Synapse-Side Changes

### Config source switch

Add `multi_tenant.source` to `homeserver.yaml`:

```yaml
multi_tenant:
  enabled: true
  source: database          # "database" or "yaml" (default for backward compat)
  reload_secret: "shared-bearer-token"
```

When `source: database`:
- Skip YAML tenant list parsing in `TenantsConfig.read_config()`
- Skip the zero-tenant guard (`ConfigError("no tenants configured")`)
- Load tenants from `public.tenants` table instead

### `TenantConfig.from_db_row()` — `synapse/config/tenants.py`

New classmethod on the frozen dataclass. Identical to `from_dict()` except:
- Sets `signing_key_path = None` (key comes from DB, not filesystem)
- Populates new field `signing_key_data: bytes | None` with the decrypted key material
- Parses JSONB sub-config columns through existing `TenantEmailConfig.from_dict()` etc.

### DB loader — `synapse/tenant_registry.py`

New `load_tenants_from_database(db_pool, master_key) -> MultiTenantConfig`:
1. `SELECT * FROM public.tenants WHERE status = 'active'`
2. Decrypt signing keys using `SYNAPSE_TENANT_KEY_MASTER` env var
3. Build `TenantConfig` via `from_db_row()`
4. Return `MultiTenantConfig`

### Keyring update — `synapse/crypto/multitenant_keyring.py`

Add `_load_tenant_keys_from_data()` that reads key material from in-memory bytes (via `StringIO` + `read_signing_keys()`) when `signing_key_path is None` and `signing_key_data` is set.

### Reload endpoint — `synapse/rest/admin/tenants.py`

`POST /_synapse/admin/v1/tenants/reload`

Auth: bearer token validated against `multi_tenant.reload_secret` from config.

Handler:
1. `load_tenants_from_database()`
2. `registry.reload(new_config)` — existing method handles add/remove/reactivate
3. Cascade to `keyring.reload()`, `ratelimiter_registry.reload()`, `app_service_registry.reload()`
4. Return `{"added": [...], "removed": [...], "unchanged": [...]}`

### Key encryption — `synapse/crypto/tenant_key_encryption.py`

New module:
- `encrypt_signing_key(key_text: str, master_key: bytes) -> bytes` — AES-256-GCM. Returns `IV (12B) || ciphertext || tag (16B)`.
- `decrypt_signing_key(encrypted: bytes, master_key: bytes) -> str` — reverse.

Master key: 32 bytes, base64-encoded in `SYNAPSE_TENANT_KEY_MASTER` env var.

Both Python (`cryptography` lib `AESGCM`) and TypeScript (`crypto.createCipheriv`) implement the same scheme.

## Control Plane Service

### Project structure — `docker-demo/control-plane/`

```
src/
  index.ts                    # Fastify app entry
  config.ts                   # Zod-validated env config
  routes/
    tenants.ts                # CRUD + lifecycle endpoints
    health.ts                 # GET /api/health
  services/
    provisioning.ts           # Orchestrates create/update/delete pipeline
    schema-cloner.ts          # Port of create_tenant_schema.py
    key-manager.ts            # Ed25519 generation + AES-256-GCM encrypt/decrypt
    synapse-client.ts         # HTTP client for Synapse reload push
  db/
    schema.ts                 # Drizzle ORM tenants table
    connection.ts             # pg pool
  middleware/
    auth.ts                   # Bearer token validation
  Dockerfile
  package.json
  tsconfig.json
  drizzle.config.ts
```

### API

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `POST` | `/api/v1/tenants` | Create tenant (full provisioning) |
| `GET` | `/api/v1/tenants` | List tenants |
| `GET` | `/api/v1/tenants/:server_name` | Get tenant details |
| `PATCH` | `/api/v1/tenants/:server_name` | Update tenant config |
| `DELETE` | `/api/v1/tenants/:server_name` | Soft-delete (status → deleting) |
| `POST` | `/api/v1/tenants/:server_name/suspend` | Suspend tenant |
| `POST` | `/api/v1/tenants/:server_name/activate` | Reactivate tenant |
| `GET` | `/api/v1/health` | Health check |

### Provisioning pipeline (`POST /api/v1/tenants`)

1. Validate input (server_name format + uniqueness)
2. Insert row with `status = 'provisioning'`
3. Generate ed25519 signing key (tweetnacl) + secrets (macaroon, form — random hex)
4. Encrypt signing key with AES-256-GCM, store in DB
5. Clone DB schema from `public` (port of `create_tenant_schema.py` — tables, sequences, singleton rows)
6. Create media directory
7. Update row → `status = 'active'`
8. `POST` to `http://synapse:8008/_synapse/admin/v1/tenants/reload`
9. Return result with step statuses

If step 5/6 fails, row stays `provisioning` — retryable. Step 8 failure is non-fatal (Synapse picks up on next restart or manual reload).

### Authentication

Bearer token from `CONTROL_PLANE_API_TOKEN` env var. Validated in Fastify preHandler hook.

## Implementation Phases

| Phase | Scope | Depends on | Key files |
|-------|-------|-----------|-----------|
| **7A** | DB schema + Synapse DB loader | — | `tenants.py`, `tenant_registry.py` |
| **7B** | Key encryption library (Python + TS) | — | `tenant_key_encryption.py`, `key-manager.ts` |
| **7C** | Synapse reload HTTP endpoint | 7A | `tenants.py` (admin API) |
| **7D** | Control plane skeleton (Fastify + Drizzle + auth + CRUD) | 7B | `docker-demo/control-plane/` |
| **7E** | Provisioning pipeline (schema clone, key gen, reload push) | 7C, 7D | `provisioning.ts`, `schema-cloner.ts` |
| **7F** | Docker-compose integration + manager UI migration | 7E | `docker-compose.yml`, `manager/` |
| **7G** | Cleanup — deprecate YAML tenants, update docs | 7F | `CLAUDE.md`, `docs/` |

**7A and 7B can run in parallel.** Everything converges at 7E.

## Verification

### Per-phase tests
- **7A:** Unit test `TenantConfig.from_db_row()` with mock DB rows. Integration test: Synapse boots with `source: database` and 0 tenants.
- **7B:** Round-trip encrypt/decrypt unit tests in both Python and TypeScript. Cross-language compat test (encrypt in TS, decrypt in Python).
- **7C:** Integration test: call reload endpoint, verify registry updated.
- **7D:** API endpoint tests with Fastify inject.
- **7E:** Full provisioning integration test: POST to create tenant, verify schema exists, verify Synapse serves `/_matrix/client/versions` for new Host.

### End-to-end (7F)
```bash
cd docker-demo
docker-compose up -d
# Synapse starts with 0 tenants
curl http://localhost:3001/api/health  # control plane healthy
# Create a tenant
curl -X POST http://localhost:3001/api/v1/tenants \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"server_name": "acme.localhost"}'
# Verify Synapse serves the tenant
curl -H "Host: acme.localhost" http://localhost:8008/_matrix/client/versions
# List tenants
curl http://localhost:3001/api/v1/tenants -H "Authorization: Bearer $TOKEN"
```

## Rejected Alternatives

**Synapse-embedded control plane** — Violates separation of concerns. Mixes Matrix homeserver with tenant management. Can't evolve independently. Rejected.

**JSONB-only tenant table** — Maximum flexibility but no DB constraints, no queryability, secrets mixed into blob. Inferior to hybrid approach. Rejected.

**RabbitMQ event bus** — Adds infrastructure. HTTP push is simpler for the initial version. Can add later for multi-worker scenarios. Deferred.
