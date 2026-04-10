# Multi-Tenant Synapse — Roadmap Progress Report

**Date:** 2026-04-10 (DB tuning phase added — 9th reconciliation)
**Branch:** `feature/multi-tenant`
**Companion to:** `docs/multi_tenant_roadmap.md` (the canonical roadmap),
`docs/multi_tenant.md` (design), and the `multi-tenancy-workflow/`
animated explainer.

This document is an audit of what has actually landed against the
phasing in `docs/multi_tenant_roadmap.md`. It exists so management can
cross-reference progress against the plan and decide whether the
remaining scope or sequencing needs adjustment.

---

## Executive summary

| Roadmap phase | Status | Notes |
|---|---|---|
| **1. Audit & instrumentation** | ✅ Complete | Schema-per-tenant isolation made real (bootstrap clones tables + sequences), `search_path` fall-through closed, `public_baseurl` / `identity_server` / `hs.hostname` request-path readers swept. See Phase 1 close section below. |
| **2. Background processes made tenant-aware** | ✅ Complete | All sub-phases closed 2026-04-09. **2a**: state_groups DictionaryCaches tenant-keyed (cross-cutting item 7 resolved). **2b**: 9 singleton seed rows cloned into tenant schemas — `user_directory` + `stats` probes green. **2c-A**: presence loop fan-out (3 loops: `_handle_timeouts`, `_persist_unpersisted_changes`, `notify_new_event`). **2c-B**: `ServerNoticesManager` resolves sender MXID per-request via tenant ContextVar — stored-row corruption fixed. Full probe suite: 34/0. Pushers deferred to phase 3. |
| **3. Per-tenant SSO / email / push / identity** | ✅ Complete | All sub-phases closed 2026-04-09. **3b**: `TenantEmailConfig` dataclass, `SendEmailHandler`/`Mailer`/`PusherFactory` resolve from tenant context, server_notices MXID sweep (6 handlers). **3a**: `TenantOidcConfig`/`TenantCasConfig`/`TenantSamlConfig` dataclasses, `OidcHandler` per-tenant provider dispatch, `CasHandler`/`SamlHandler` lazy resolution. **3c**: `TenantPushConfig` dataclass, `HttpPusher`/`EmailPusher`/`BulkPushRuleEvaluator` resolve from tenant context. 21 unit tests pass. |
| **4. Per-tenant rate limiting + app services** | ✅ Complete | **4a**: `TenantRatelimitConfig` (17 `RatelimitSettings` fields), `TenantRatelimiterRegistry` singleton, 12 handler/REST files converted to resolve limiters from tenant context. **4b**: `TenantAppServiceRegistry` loads per-tenant AS lists at startup, store methods (`get_app_services`, `get_app_service_by_user_id`, `get_app_service_by_token`, `get_if_app_services_interested_in_user`) dispatch on `get_current_tenant()`. 28 unit tests pass. |
| **5. File storage providers** | ✅ Complete | `FileStorageProviderBackend._tenant_base` resolves `<base>/<server_name>/` for store/fetch. `MediaStorage._local_path` replaces 4 global join sites. `MediaRepository` wired to `MultiTenantMediaFilePaths` via factory. URL previewer + thumbnailer audited — no bypasses. 8 probes green. S3 provider deferred (future: single shared bucket, `server_name` key prefix). |
| **6. Hot add/remove + backup/restore** | ✅ Complete | **6a**: SIGHUP-triggered hot reload — `TenantRegistry.reload()`, `MultiTenantKeyring.reload()`, `TenantAppServiceRegistry.reload()`, `TenantRatelimiterRegistry.reload()` with in-place mutable-registry pattern. Inactive tenants filtered from all query methods. **6b**: `synapse_tenant backup`/`restore`/`drop` CLI subcommands (pg_dump/psql + media tar). 16 probes green. |
| **7. Dynamic tenant control plane** | ✅ Complete | Database-driven tenancy replaces YAML tenant list. Standalone TypeScript/Fastify control plane service handles full tenant lifecycle (create, suspend, activate, delete). AES-256-GCM encrypted signing keys in DB. Synapse boots with zero tenants and loads from `public.tenants` table. HTTP-push reload endpoint. See Phase 7 section below. |
| **8. Database tuning & connection optimization** | ⏸ Not started | Three sub-phases: **8a** `SET LOCAL search_path` (eliminate SHOW + restore, ~10 lines), **8b** connection-level schema caching (skip SET if unchanged, ~50 lines), **8c** pool tuning + pgbouncer sidecar. Target: raise ceiling from ~10-15 to ~100-200 tenants. See `challenges.md` for capacity estimates. |
| **9. Federation outbound** | ⏸ Not started | |
| **10. Federation inbound** | ⏸ Not started | |
| **11. E2EE audit pass** | ⏸ Not started | |
| **12. Workers** | ⏸ Not started | |
| **13. Optional hardening** | ⏸ Not started | Full per-tenant SAML `Saml2Client` SP construction, per-tenant email templates, storage-layer `_server_notices_mxid` cache. |

In addition to the roadmap-listed phases, two pieces of infrastructure
landed that are *not* phases of their own but are listed here because
management cares about them:

- A **second self-contained docker rig** (`docker-demo/`) intended for
  Element-driven manual demos with two real-looking domain names
  behind Traefik, with optional `mkcert` HTTPS.
- A **standalone control plane service** (`docker-demo/control-plane/`)
  — TypeScript/Fastify/Drizzle — that manages the full tenant lifecycle
  via REST API. Synapse reads tenants from a `public.tenants` DB table
  instead of YAML. Integrated into `docker-compose.yml` on port 3001.
- An **animated workflow explainer** (`multi-tenancy-workflow/`) kept
  in sync with the bg-process work for stakeholder presentations.

---

## Phase 1 — Audit & instrumentation

Roadmap reference: `docs/multi_tenant_roadmap.md` §"Observability &
per-request audit (phase 1, mostly done)" and §"Suggested phasing" item
1.

### Landed in this branch

**Pre-existing instrumentation (already in the fork before this audit):**

- Per-request `LoggingContext` carries
  `server_name=effective_server_name` (`synapse/http/site.py`).
- `requests_counter` and the upstream metrics that use the
  `SERVER_NAME_LABEL` automatically pick up the active tenant.

**Phase-1 deltas:**

- `docker-multitenant/` rig fitted with a Prometheus container scraping
  `/_synapse/metrics`, file log handler under `data/logs/synapse.log`,
  and `OBSERVABILITY` + `PHASE 1 LEAK PROBES` blocks in
  `scripts/test_tenants.py`.
- Hot-iteration bind mounts on the `synapse` service: a Python edit +
  `docker-compose up -d synapse` is the full inner loop.
- `%(server_name)s` added to `docker-multitenant/config/log.config` so
  the per-request server_name actually appears in formatted output.
- **`HomeServer.effective_server_name()`** added in
  `synapse/server.py`. Returns the active tenant's `server_name` when
  a tenant context is bound, otherwise `self.hostname`. Canonical
  accessor for code that previously read `self.hs.hostname` directly.
- **Login leak fixed.** `synapse/rest/client/login.py` (3 sites) and
  `synapse/handlers/auth.py` (2 sites) were qualifying bare localparts
  and constructing `LoginResponse.home_server` against
  `self.hs.hostname` — so a login to `acme.localhost` produced
  `@user:localhost`, missed the tenant schema, and returned 403
  "Invalid username or password". All five sites now go through
  `hs.effective_server_name()`.

### Verification

End-to-end via the `docker-multitenant` rig on 2026-04-07:

    synapse_http_server_requests_received_total{
        method="GET",
        server_name="acme.localhost",
        servlet="VersionsRestServlet"
    } 3.0

    [server_name=acme.localhost] synapse.access.http.8008 - INFO -
        Processed request: ... GET /_matrix/client/versions ...

    [PASS] login - home_server=acme.localhost
                   user_id=@test_acme_…:acme.localhost
    [PASS] CapabilitiesRestServlet metric carries per-tenant server_name

### Gaps still owed against phase 1

- **`/.well-known/matrix/client` builder** reads `public_baseurl` from
  the global config — every tenant gets the same announced base URL.
  Closing this requires adding `public_baseurl` (and possibly
  `identity_server`) to `TenantConfig`, plus auditing every other
  reader of `hs.config.server.public_baseurl` (password-reset email
  links, registration emails, identity-server discovery). Sized as a
  small standalone task. Tracked by the failing
  `test_wellknown_client_per_tenant` probe.
- 2–3 additional leak probes recommended before closing phase 1:
  registration response, password-reset email link,
  `/_matrix/federation/v1/version`.
- The background-process server_name fallback
  (`unknown_server_from_sentinel_context`) is mechanically resolved by
  phase 2 (below) and is not a separate phase-1 ticket.

---

## Phase 2 — Tenant-aware background processes

Roadmap reference: `docs/multi_tenant_roadmap.md` §"Tenant-aware
background processes (phase 2, in progress)" and §"Suggested phasing"
item 2.

### Foundation (landed 2026-04-07)

- **`synapse/tenant_background.py`** —
  `run_as_background_process_per_tenant(desc, hs, func, …)`. Iterates
  the registry, binds `tenant_context()` *inside* each scheduled
  coroutine (so the `ContextVar` survives Twisted's `ensureDeferred`
  boundary), schedules one `run_as_background_process` per tenant,
  and falls back to a single-process call labelled with `hs.hostname`
  when multi-tenant mode is disabled. This is the DRY helper every
  subsequent bg-process conversion goes through.
- **`hs.get_tenant_registry()`** cached singleton on `HomeServer`,
  backed by `create_tenant_registry(self)`. Prior to this the registry
  was only reachable via `SynapseSite`; background processes now have
  a clean DI access path.

### Loops converted

All seven of these go through `run_as_background_process_per_tenant`
and are exercised by the green probes in
`docker-multitenant/scripts/test_tenants.py`'s `PHASE 2 PROBES` block.

| File | Loop | Notes |
|---|---|---|
| `synapse/handlers/user_directory.py` | `notify_new_event` | `pos` and `_is_processing` were per-instance globals — converted to per-tenant dicts. `_unsafe_process` reads `effective_server_name` for `Measure` blocks and `event_processing_positions` metric labels from `get_current_tenant()` instead of constructor-time `self.server_name`. |
| `synapse/handlers/stats.py` | stats stream loop | Same pattern: per-tenant `pos` and `_is_processing`, `event_processing_positions{name="stats"}` now per-tenant labelled. |
| `synapse/handlers/pagination.py` | retention purge `looping_call` | Verified in logs as `[server_name=acme.localhost] synapse.handlers.pagination … [purge] …` on a 3 s test interval. |
| `synapse/handlers/account_validity.py` | `send_renewals` (email-based renewal sweep) | `@wrap_as_background_process` decorator dropped on the inner method in favour of per-tenant fan-out. |
| `synapse/handlers/deactivate_account.py` | `_user_parter_loop` (resume on startup) | The "already running" guard converted from a single in-process flag to a per-tenant set (`_user_parter_running_tenants`) so two tenants can part users concurrently. Request-path entry point relies on `hs.run_as_background_process` to carry the caller's tenant through the spawn. |
| `synapse/handlers/device.py` | `delete_stale_devices` and `_maybe_retry_device_resync` | Both `looping_call` sites converted to the per-tenant helper. |
| `synapse/handlers/auth.py` | `expire_old_sessions` | Replaced; the now-unused `run_as_background_process` import was removed. |
| `synapse/handlers/message.py` | `send_dummy_events_to_fill_extremities` | Converted from the prior `lambda → hs.run_as_background_process` shape to per-tenant fan-out. |

### Verification

`docker-multitenant/scripts/test_tenants.py` `PHASE 2 PROBES` block,
green on 2026-04-07:

    [PASS] user_directory populated per tenant
           (@test_acme_…:acme.localhost findable in own tenant)
    [PASS] stats loop ran per tenant
           (event_processing_positions{name='stats'} present for
            acme.localhost, corp.localhost)
    [PASS] retention purge ran per tenant
           (background_process_start_count
            {name='purge_history_for_rooms_in_range'} present for
            acme.localhost, corp.localhost, startup.localhost)
    [PASS] user parter loop ran per tenant at startup
           (background_process_start_count{name='user_parter_loop'}
            present for all tenants)

Full unit-test suite under `tests/tenant/` is green (28 tests).

### Gaps still owed against phase 2

- **`synapse/handlers/presence.py`** — presence update / timeout loops.
  Not yet converted.
- **`synapse/push/pusherpool.py`, `emailpusher.py`, `httppusher.py`** —
  per-user pushers, scheduling loop currently global. Pushers belong
  to a specific user and the user already pins the tenant, so the
  fan-out shape may need to differ from the `notify_new_event` style.
- **`synapse/handlers/user_directory.py:614` —
  `kick_off_remote_profile_refresh_process`** (startup + periodic
  refresh). Deferred because the inner
  `kick_off_remote_profile_refresh_process_for_remote_server` takes a
  *remote* server_name argument, so per-tenant fan-out semantics need
  re-thinking (each tenant has its own view of "remote"). Belongs in
  the federation phase regardless.
- Storage-layer background updaters
  (`synapse/storage/database.py`, `synapse/storage/background_updates.py`)
  — deliberately *not* converting in phase 2: they operate on the
  whole DB, not per-tenant rows, and the schema bootstrap step already
  runs them against each tenant schema.

---

## Phase 3 — Per-tenant SSO / email / push / identity

Closed 2026-04-09. All three sub-phases shipped.

### 3a — Per-tenant SSO

Config dataclasses added to `synapse/config/tenants.py`:
- `TenantOidcConfig` — stores raw provider dicts, parsed by `_parse_oidc_provider_configs`
- `TenantCasConfig` — `server_url`, `displayname_attribute`, `required_attributes`
- `TenantSamlConfig` — `idp_name`, `idp_entityid`, other SAML settings

Handler conversions:
- `synapse/handlers/oidc.py` — per-tenant OIDC provider dispatch via `_get_oidc_providers_for_tenant()`
- `synapse/handlers/cas.py` — lazy CAS config resolution from tenant context
- `synapse/handlers/saml.py` — lazy SAML config resolution from tenant context

### 3b — Per-tenant email + server_notices sweep

- `TenantEmailConfig` dataclass (SMTP host/port/auth, TLS, `notif_from`, etc.)
- `synapse/handlers/send_email.py` — `SendEmailHandler` resolves SMTP config from tenant context
- Server notices MXID sweep: 6 handlers converted to use `tenant.effective_server_notices_mxid`

### 3c — Per-tenant push

- `TenantPushConfig` dataclass (`include_content`, `enabled`, `group_unread_count_by_room`, `jitter_delay_ms`)
- `synapse/push/httppusher.py`, `synapse/push/emailpusher.py`, `synapse/push/bulk_push_rule_evaluator.py` resolve push config from tenant context

### Verification

21 unit tests across `tests/tenant/test_tenant_email_config.py`, `tests/tenant/test_tenant_sso_config.py`, `tests/tenant/test_tenant_push_config.py`.

---

## Phase 4 — Per-tenant rate limiting + app services

Closed 2026-04-09. Two independent sub-phases.

### 4a — Per-tenant rate limiting

**Problem:** All ~15 `Ratelimiter` instances created once at startup from global `hs.config.ratelimiting.*`. Users on different tenants share the same buckets and the same `per_second`/`burst_count` settings.

**Solution:** `TenantRatelimitConfig` frozen dataclass with 17 `RatelimitSettings | None` fields (each `None` = inherit global). `TenantRatelimiterRegistry` singleton on `HomeServer` lazily creates per-tenant `Ratelimiter` instances cached by `(server_name, limiter_key)`.

Files converted (12 handler/REST sites):

| File | Limiters |
|---|---|
| `synapse/handlers/room_member.py` | `rc_joins_local`, `rc_joins_remote`, `rc_joins_per_room`, `rc_invites_per_room`, `rc_invites_per_user`, `rc_invites_per_issuer`, `rc_third_party_invite` |
| `synapse/rest/client/login.py` | `rc_login_address`, `rc_login_account` |
| `synapse/handlers/auth.py` | `rc_login_failed_attempts` (UIA + login) |
| `synapse/handlers/identity.py` | `rc_3pid_validation` |
| `synapse/handlers/devicemessage.py` | `rc_key_requests` |
| `synapse/rest/client/register.py` | `rc_registration_token_validity` |
| `synapse/rest/client/sync.py` | `rc_presence_per_user` |
| `synapse/rest/client/presence.py` | `rc_presence_per_user` |
| `synapse/rest/client/user_directory.py` | `rc_user_directory` |
| `synapse/rest/media/create_resource.py` | `rc_media_create` |
| `synapse/media/media_repository.py` | `remote_media_downloads` |

Not converted (by design):
- `synapse/rest/client/login_token_request.py` — hardcoded security-bound rate limit, not configurable per-tenant
- `synapse/api/ratelimiting.py:RequestRatelimiter` — tightly coupled to per-user DB override logic

### 4b — Per-tenant app services

**Problem:** `ApplicationServiceWorkerStore` loads all AS config files once at startup into a single global `services_cache`. All tenants share the same bridges/bots.

**Solution:** `TenantAppServiceRegistry` loads per-tenant AS lists at startup from `TenantConfig.app_service_config_files`. Store methods dispatch on `get_current_tenant()`:

| Method | Tenant-aware? |
|---|---|
| `get_app_services()` | ✅ Returns tenant's AS list |
| `get_app_service_by_user_id()` | ✅ Searches tenant's AS list |
| `get_if_app_services_interested_in_user()` | ✅ Uses tenant's exclusive regex |
| `get_app_service_by_token()` | ✅ Searches tenant's AS list |
| `get_app_service_by_id()` | Global (used by AS transaction logic) |

### Verification

28 unit tests across `tests/tenant/test_tenant_ratelimit_config.py` (7), `tests/tenant/test_tenant_ratelimiter_registry.py` (6), `tests/tenant/test_tenant_appservice.py` (10), `tests/tenant/test_config.py` (5).

---

## Infrastructure additions (not a roadmap phase)

### `docker-demo/` — Element-facing 2-tenant demo rig

A second, simpler docker rig was added on 2026-04-07 / 2026-04-08
specifically for **manual Element-based demos** with realistic-looking
domain names (rather than the `*.localhost` suffix used by
`docker-multitenant/`). It is *not* a replacement for
`docker-multitenant/` — that one stays the developer/test rig with
nginx, Prometheus, the leak-probe suite, and 3 tenants. `docker-demo/`
is intentionally minimal:

- **Two tenants:** `matrix.tenant-a.com` and `matrix.tenant-b.com`.
- **Reverse proxy:** Traefik v2.11 (replaces nginx) on standard ports
  80 / 443. The switch to Traefik was driven by the user's requirement
  to drop the `:8080` suffix from URLs — Element's well-known
  discovery flow re-targets the URL it finds, so the demo would only
  work end-to-end on standard ports.
- **TLS:** optional, opt-in via `mkcert`. The Traefik routers are
  split — `synapse@web` always serves HTTP, `synapse-tls@websecure`
  serves HTTPS only when the cert exists. Enabled today via:

      cd docker-demo
      mkcert -install
      mkdir -p certs
      mkcert -cert-file certs/tenants.crt \
             -key-file  certs/tenants.key \
             matrix.tenant-a.com matrix.tenant-b.com
      docker compose restart traefik

  This is what unblocked Element Web (chrome refused mixed HTTP
  homeserver content from an HTTPS-served `app.element.io`).

- **Schema cloning:** a `copy-schemas` one-shot service runs after
  Synapse becomes healthy and clones the public-schema table structure
  + singleton seed rows into each tenant_* schema using
  `CREATE TABLE … LIKE public.<table> INCLUDING ALL`. **This
  surfaced a pre-existing bug:** tenant schemas were empty in *both*
  the demo and `docker-multitenant/` rigs — all 168 tables only
  existed in `public`, and `search_path` was silently falling through.
  The leak-probe suite happened to pass anyway because it used unique
  timestamped usernames per tenant. The demo broke it because
  registering `alice` on tenant A and tenant B both wrote to
  `public.profiles` and tripped `profiles_user_id_key`. The fix is
  shipped only in `docker-demo/` for now; the same fix needs to be
  back-applied to `docker-multitenant/` and re-considered as part of
  `scripts/create_tenant_schema.py` (see "Cross-cutting items" below).

**Files added:**

- `docker-demo/docker-compose.yml` — Traefik + Postgres + keygen +
  init-schemas + synapse + copy-schemas, plus full hot-reload bind
  mounts of every patched Python file used by phases 1 and 2.
- `docker-demo/config/homeserver.yaml` — minimalist 2-tenant config
  (`server_name`, `database_schema`, `signing_key_path`,
  `media_store_path`, `public_baseurl` per tenant).
- `docker-demo/traefik/dynamic/tls.yml` — TLS certificate store
  pointing at `/certs/tenants.crt` (only honoured once `mkcert` has
  run).
- `docker-demo/scripts/{generate_keys.py, init_schemas.py,
  copy_tenant_tables.py}` — bootstrap helpers (the third one is the
  schema-cloning fix described above).
- `docker-demo/README.md` — explains the Traefik setup, the
  `mkcert` opt-in path, and the port-80 conflict with the
  `docker-multitenant/` rig (only one of them can hold port 80 at a
  time).

**Diagnostics that fed back into the demo:**

- The Traefik `docker` provider was discovering the synapse container
  but couldn't pick a network IP to dial (the project has multiple
  networks). Fixed by adding `--providers.docker.network` on Traefik
  and a matching `traefik.docker.network` label on synapse.
- A single router with `tls=true` and both `web,websecure` entrypoints
  was silently dropping the HTTP entrypoint when no cert was present.
  Fixed by splitting into `synapse@web` (no TLS) and
  `synapse-tls@websecure` (TLS) so the HTTP path stays up regardless
  of cert state.

### `multi-tenancy-workflow/` — animated explainer

`data.js` was updated alongside the bg-process conversions so the
explainer's "background processes" stage shows the loops that have
actually been converted (retention purge, `send_renewals`,
`user_parter_loop`, etc.). This is a presentation asset, not part of
Synapse itself.

---

## Phase 5 — File storage providers

Roadmap reference: `docs/multi_tenant_roadmap.md` §"Suggested phasing"
item 5.

### Landed in this branch

**Sub-phase 5a — Tenant-aware local FS provider + MediaStorage:**

- `FileStorageProviderBackend._tenant_base(base)` — resolves
  `<base>/<server_name>` when a tenant context is active, falls back
  to `<base>` in single-tenant mode.
  (`synapse/media/storage_provider.py`)
- `store_file` and `fetch` updated to route through `_tenant_base`
  for both `cache_directory` and `base_directory`.
- `MediaStorage._local_path(rel_path)` — inserts tenant `server_name`
  between `local_media_directory` and the relative path. Replaces 4
  `os.path.join(self.local_media_directory, path)` sites in
  `store_into_file`, `fetch_media`, and `ensure_media_is_in_local_cache`
  (×2, including legacy thumbnail fallback).
  (`synapse/media/media_storage.py`)
- `MediaRepository.__init__` switched from `MediaFilePaths(...)` to
  `create_media_file_paths(...)` factory, which returns
  `MultiTenantMediaFilePaths` when `hs.config.tenants.multi_tenant.enabled`
  is true. (`synapse/media/media_repository.py`)

**Sub-phase 5b — URL preview / thumbnail audit:**

- `url_previewer.py`: all media storage operations go through
  `MediaStorage.store_into_file` which uses `_local_path` (tenant-aware).
  `self.primary_base_path` is a dead reference — stored but never read.
  No bypasses found.
- `thumbnailer.py`: all media storage operations go through
  `MediaStorage.fetch_media`, `store_into_file`,
  `ensure_media_is_in_local_cache` — all tenant-aware. No direct
  `os.path.join` with media directories. No bypasses found.

### Probe results

| Test | Module | Result |
|---|---|---|
| `test_store_file_with_tenant_writes_under_tenant_dir` | `test_storage_provider` | PASS |
| `test_tenant_base_without_tenant_returns_base` | `test_storage_provider` | PASS |
| `test_tenant_isolation_different_tenants` | `test_storage_provider` | PASS |
| `test_local_path_with_tenant` | `test_media_storage` | PASS |
| `test_local_path_without_tenant` | `test_media_storage` | PASS |
| `test_local_path_isolation` | `test_media_storage` | PASS |
| `test_returns_multitenant_when_enabled` | `test_media_filepath` | PASS |
| `test_returns_plain_when_disabled` | `test_media_filepath` | PASS |

Total: **8/0** (8 pass, 0 fail).

### Gaps still owed

- **S3 storage provider** — deferred from phase 5. Future model: single
  shared bucket, tenant `server_name` as S3 key prefix (e.g.
  `s3://synapse-media/acme.com/local_content/...`). Provider calls
  `get_current_tenant()` inside `store_file`/`fetch` to resolve the
  prefix. `boto3` as optional dependency. Minimal in-tree
  implementation, not vendoring `synapse-s3-storage-provider`.
- **`url_previewer.py` dead reference** — `self.primary_base_path`
  (line 183) is stored but never used. Harmless but could be cleaned up.

---

## Phase 7 — Dynamic tenant control plane

Roadmap reference: `docs/superpowers/specs/2026-04-09-dynamic-tenant-control-plane-design.md`.
Closed 2026-04-09.

### Problem

Phases 1–6 relied on `homeserver.yaml` as the source of truth for
tenants. Adding a tenant required editing the YAML, generating a signing
key on disk, cloning the DB schema, and sending SIGHUP — a manual,
error-prone workflow that cannot be exposed as a SaaS API. Zero-tenant
boot was impossible (the config guard required at least one tenant).

### Solution

A standalone **control plane service** (`docker-demo/control-plane/`)
manages the full tenant lifecycle via REST API. Synapse reads tenants
from a `public.tenants` database table instead of YAML.

### Sub-phase 7A — DB schema + Synapse DB loader

- `public.tenants` table: UUID PK, `server_name`/`database_schema`
  unique constraints, `status` CHECK (`provisioning`/`active`/
  `suspended`/`deleting`), BYTEA for encrypted signing key, typed
  columns for core config, JSONB for complex sub-configs (OIDC, SAML,
  rate limits, etc.), audit timestamps, `config_version`.
- `MultiTenantConfig.source` field: `"yaml"` (default, backward compat)
  or `"database"`. Database source skips the zero-tenant config guard.
- `TenantConfig.from_db_row()` classmethod: parses a DB row dict into
  a frozen `TenantConfig`, including JSONB sub-config columns.
- `TenantConfig.signing_key_data` field: holds in-memory key text for
  DB-sourced tenants (no filesystem path needed).
- `load_tenants_from_database()` in `synapse/tenant_registry.py`:
  queries `public.tenants WHERE status = 'active'`, decrypts signing
  keys, builds `MultiTenantConfig`.

### Sub-phase 7B — Key encryption library

AES-256-GCM encryption for signing keys, cross-language compatible
between Python and TypeScript.

- **Python** (`synapse/crypto/tenant_key_encryption.py`):
  `encrypt_signing_key()` / `decrypt_signing_key()` using
  `cryptography.hazmat.primitives.ciphers.aead.AESGCM`. Master key
  from `SYNAPSE_TENANT_KEY_MASTER` env var (base64, 32 bytes).
- **TypeScript** (`control-plane/src/services/key-manager.ts`):
  same AES-256-GCM scheme via `crypto.createCipheriv`. Ed25519 key
  generation via `tweetnacl`.
- Wire format: `IV(12B) || ciphertext || tag(16B)`.

### Sub-phase 7C — Synapse reload HTTP endpoint

- `POST /_synapse/admin/v1/tenants/reload` in
  `synapse/rest/admin/tenants.py`. Bearer token auth against
  `multi_tenant.reload_secret` (separate from Matrix admin auth — the
  control plane is not a Matrix user).
- For `source: database`: gets master key from env, queries
  `public.tenants`, decrypts keys, cascades reload to registry,
  keyring, app service registry, ratelimiter registry.
- For `source: yaml`: re-reads config file (preserves existing SIGHUP
  behavior via HTTP).

### Sub-phase 7D — Control plane skeleton

Standalone TypeScript service: **Fastify** (HTTP framework) +
**Drizzle ORM** (type-safe DB access) + **Zod** (request validation).

- `src/db/schema.ts` — Drizzle schema matching `public.tenants` DDL.
- `src/routes/tenants.ts` — full CRUD: POST create, GET list, GET by
  server name, PATCH update, DELETE soft-delete, POST suspend, POST
  activate.
- `src/middleware/auth.ts` — bearer token validation (shared secret).
- `src/config.ts` — Zod-validated env vars.

### Sub-phase 7E — Provisioning pipeline

The control plane orchestrates tenant creation end-to-end:

1. Validate server name (regex: hostname format).
2. Generate Ed25519 signing key.
3. Encrypt signing key with master key.
4. Insert tenant row (`status: provisioning`).
5. Create PostgreSQL schema + clone table structure from `public`
   (skipping the `tenants` table itself), clone sequences, copy
   singleton seed rows.
6. Create media directory.
7. Update tenant status to `active`.
8. Push reload to Synapse via HTTP.

Each step is reported in the API response for observability.

Schema cloning (`src/services/provisioning.ts`) ports the logic from
`scripts/copy_tenant_tables.py` into TypeScript with parameterised
queries and schema-name validation to prevent SQL injection.

### Sub-phase 7F — Docker-compose integration

- `docker-compose.yml`: added `control-plane` service (port 3001) with
  `DATABASE_URL`, `API_TOKEN`, `TENANT_KEY_MASTER`, `SYNAPSE_URL`,
  `SYNAPSE_RELOAD_SECRET`, `MEDIA_BASE_DIR`.
- `homeserver.yaml`: switched to `source: database`, added
  `reload_secret`.

### Probe results

| Test | Module | Result |
|---|---|---|
| `test_from_db_row_round_trip` | `test_config_db_row` | PASS |
| `test_from_db_row_missing_server_name` | `test_config_db_row` | PASS |
| `test_from_db_row_missing_database_schema` | `test_config_db_row` | PASS |
| `test_from_db_row_optional_fields_default` | `test_config_db_row` | PASS |
| `test_from_db_row_with_oidc_config` | `test_config_db_row` | PASS |
| `test_from_db_row_with_cas_config` | `test_config_db_row` | PASS |
| `test_from_db_row_with_saml_config` | `test_config_db_row` | PASS |
| `test_from_db_row_with_email_config` | `test_config_db_row` | PASS |
| `test_from_db_row_with_push_config` | `test_config_db_row` | PASS |
| `test_from_db_row_with_ratelimit_config` | `test_config_db_row` | PASS |
| `test_from_db_row_signing_key_data` | `test_config_db_row` | PASS |
| `test_from_db_row_frozen` | `test_config_db_row` | PASS |
| `test_multi_tenant_config_source_field` | `test_config_db_row` | PASS |
| `test_multi_tenant_config_reload_secret` | `test_config_db_row` | PASS |
| `test_database_source_allows_zero_tenants` | `test_config_db_row` | PASS |
| `test_yaml_source_requires_tenants` | `test_config_db_row` | PASS |
| `test_invalid_source_raises` | `test_config_db_row` | PASS |
| `test_encrypt_decrypt_round_trip` | `test_key_encryption` | PASS |
| `test_different_ivs` | `test_key_encryption` | PASS |
| `test_wrong_key_fails` | `test_key_encryption` | PASS |
| `test_tampered_data_fails` | `test_key_encryption` | PASS |
| `test_wire_format_iv_12_bytes` | `test_key_encryption` | PASS |
| `test_unicode_key_text` | `test_key_encryption` | PASS |
| `test_env_var_loading` | `test_key_encryption` | PASS |
| `test_env_var_missing` | `test_key_encryption` | PASS |
| `test_env_var_wrong_length` | `test_key_encryption` | PASS |
| `test_env_var_invalid_base64` | `test_key_encryption` | PASS |

Total: **27/0** (27 pass, 0 fail).

### Gaps still owed

- **Cross-language compat test.** Python-encrypted key decrypted by
  TypeScript and vice versa. Currently verified manually; should be
  automated.
- **End-to-end integration test.** POST to control plane → tenant
  reachable via `/_matrix/client/versions` with the new Host header.
  Deferred to the docker-demo probe rig.
- **Per-tenant JSONB sub-config round-trip.** The control plane PATCH
  endpoint accepts JSONB updates but Synapse's `from_db_row()` parsing
  of complex sub-configs (OIDC, SAML, rate limits) has not been
  integration-tested end-to-end through the reload path.

---

## Cross-cutting items surfaced during this work

These are issues or follow-ups that don't map cleanly to a single
roadmap phase but need to be on management's radar.

1. **Schema cloning bug.** Tenant schemas were silently empty in the
   `docker-multitenant/` reference rig, with `search_path` falling
   through to `public`. The leak-probe suite did not catch it because
   it uses unique-per-tenant usernames. The fix is shipped in
   `docker-demo/` via a one-shot `copy_tenant_tables.py` service. The
   same fix should be:
   - Back-applied to `docker-multitenant/`.
   - Folded into `scripts/create_tenant_schema.py` so the operator CLI
     guarantees a usable schema rather than just an empty namespace.
   - Backed by a phase-1 leak probe that registers the *same*
     localpart in two tenants and asserts both succeed.
2. **Phase-1 leak coverage is thinner than the prose suggests.** The
   schema-cloning bug is the most visible example: real isolation was
   not actually being exercised. Adding probes that share a localpart
   across tenants, share a room id, and share a media URL would
   meaningfully strengthen the audit before declaring phase 1 closed.
3. **The `effective_server_name()` migration is not finished.**
   Phases 1 and 2 fixed the call sites that the leak probes and the
   bg-process conversions actually traversed. A grep for
   `self.hs.hostname` / `self.server_name` outside the converted files
   still returns plenty of hits, and each one is a latent leak.
4. **Multiple docker rigs holding port 80.** `docker-multitenant/`
   (nginx) and `docker-demo/` (Traefik) both bind 80. This is fine
   for one developer at a time but is a footgun for CI and for anyone
   running both at once. Worth resolving by either:
   - Moving `docker-demo/` to a higher-numbered port (loses the
     "no `:8080` in the URL" property and breaks Element discovery), or
   - Putting both rigs behind a shared host-level reverse proxy.

---

## Phase 6 — Hot add/remove + backup/restore tooling

Roadmap reference: `docs/multi_tenant_roadmap.md` §"Suggested phasing"
item 6. Closed 2026-04-09.

### Sub-phase 6a — Hot reload via SIGHUP

**Problem:** Adding or removing a tenant required restarting the entire
Synapse process, causing downtime for all tenants.

**Solution:** SIGHUP triggers a reload cascade. All registries are
mutated in-place (mutable-registry pattern) so `@cache_in_self`
references on `HomeServer` remain valid.

Files changed:

| File | Change |
|---|---|
| `synapse/tenant_registry.py` | `reload(new_config)`, `is_inactive()`, `_inactive_tenants` set. `get_tenant()`, `get_all_tenants()`, `get_all_server_names()`, `is_local_server_name()` filter inactive. `preload_all_signing_keys()` and `add_hostname_alias()` also filter inactive. |
| `synapse/crypto/multitenant_keyring.py` | `reload(registry)` — loads keys for new tenants, removes keys for inactive. |
| `synapse/appservice/tenant_registry.py` | `reload(registry)` — adds/removes per-tenant AS lists. |
| `synapse/api/tenant_ratelimiting.py` | `reload(registry)` — evicts cached limiters for inactive tenants. |
| `synapse/app/_base.py` | `_reload_tenants` SIGHUP callback registered in `start()`. Uses `hs.config.reload_config_section("tenants")` to re-read YAML from disk. |

**Tenant removal safety:** Removing a tenant from config + SIGHUP marks
it inactive (requests get 404). Schema and media are preserved. Actual
cleanup requires `synapse_tenant drop --confirm-destructive`.

### Sub-phase 6b — Backup/restore CLI

**Problem:** No way to back up or restore a single tenant's data without
a full database dump.

**Solution:** Three new subcommands added to `scripts/synapse_tenant`:

| Subcommand | What it does |
|---|---|
| `backup --server-name X --output Y.tar.gz` | `pg_dump` of tenant schema + tar of media tree → single `.tar.gz` with `manifest.json` |
| `restore --server-name X --from Y.tar.gz [--overwrite]` | Extract archive, validate manifest, restore schema via `psql`, restore media tree |
| `drop --server-name X --confirm-destructive` | `DROP SCHEMA ... CASCADE` + `rm -rf` media dir. Safety flag required. |

Archive format: `manifest.json` + `schema.sql` + `media/` directory.

### Probe results

| Test | Module | Result |
|---|---|---|
| `test_reload_adds_new_tenant` | `test_registry_reload` | PASS |
| `test_reload_removes_tenant` | `test_registry_reload` | PASS |
| `test_reload_unchanged_tenants` | `test_registry_reload` | PASS |
| `test_reload_reactivates_tenant` | `test_registry_reload` | PASS |
| `test_reload_malformed_config_preserves_state` | `test_registry_reload` | PASS |
| `test_reload_inactive_not_in_get_all_tenants` | `test_registry_reload` | PASS |
| `test_reload_loads_new_tenant_keys` | `test_keyring_reload` | PASS |
| `test_reload_removes_inactive_tenant_keys` | `test_keyring_reload` | PASS |
| `test_reload_adds_new_tenant` | `test_appservice_reload` | PASS |
| `test_reload_removes_inactive_tenant` | `test_appservice_reload` | PASS |
| `test_reload_clears_removed_tenant_limiters` | `test_ratelimiter_reload` | PASS |
| `test_reload_preserves_active_tenant_limiters` | `test_ratelimiter_reload` | PASS |
| `test_archive_contains_required_files` | `test_tenant_backup` | PASS |
| `test_manifest_contents` | `test_tenant_backup` | PASS |
| `test_archive_round_trip_structure` | `test_tenant_backup` | PASS |
| `test_drop_subcommand_requires_confirm_flag` | `test_tenant_backup` | PASS |

Total: **16/0** (16 pass, 0 fail).

### Gaps still owed

- **DB pool reload.** The roadmap mentions DB pool reload; this phase
  does not change the connection pool. The pool is shared across
  tenants and `search_path` is set per-transaction, so no pool change
  is needed for tenant add/remove. If per-tenant connection pools are
  ever wanted, that belongs in a future phase.
- **End-to-end SIGHUP integration test.** The unit tests verify each
  registry's `reload()` method. A full integration test that sends
  SIGHUP to a running Synapse and verifies a new tenant is routable
  is deferred to the docker-multitenant probe rig.

---

## Suggested next steps (for management to weigh)

Ordered by what's cheapest to ship and what unblocks the most
downstream work. Phases 1–7 are complete.

1. **Phase 8–9 — Federation outbound + inbound.** Per-tenant
   `FederationSender`, federation key serving, `.well-known`
   responses. Largest remaining chunk.
2. **S3 storage provider (phase 5 follow-up).** Single shared bucket
   with `server_name` key prefix. Unblocks k8s deployments where pods
   don't share disks.
3. **Defer phases 10–12 as currently sequenced.** Nothing learned in
   phases 1–7 suggests a re-order.

---

## Verification commands (for the next reviewer)

    # Unit tests
    trial tests.tenant

    # docker-multitenant probe rig (the canonical CI surface)
    cd docker-multitenant
    docker compose up -d
    docker compose run --rm test     # runs PHASE 1 + PHASE 2 probes
    docker compose down -v

    # docker-demo Element-facing rig
    cd docker-demo
    docker compose up -d
    curl -H "Host: matrix.tenant-a.com" http://localhost/_matrix/client/versions
    # then point Element at http://matrix.tenant-a.com (or https://… if mkcert ran)
