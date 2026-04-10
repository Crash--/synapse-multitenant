# Multi-Tenancy Roadmap

Status of the Linagora multi-tenant Synapse fork (`feature/multi-tenant`) and
the work still needed before a single Synapse process can host arbitrary,
production-grade Matrix homeservers in full isolation.

Companion to `docs/multi_tenant.md` (design) and the
`multi-tenancy-workflow/` animated explainer.

---

## ✅ Implemented

The pieces below are in place and exercised by the unit tests in
`tests/tenant/` and the `docker-multitenant/` 3-tenant demo.

### Request routing

- `synapse/tenant_registry.py` — loads tenants from `multi_tenant.tenants`
  in `homeserver.yaml`, indexes them by `server_name` and `host_aliases`.
- Host-header → `TenantConfig` resolution at the front of the request
  pipeline (nginx forwards `Host`, Synapse matches it).

### Per-request tenant context

- `synapse/tenant_context.py` — `contextvars.ContextVar[TenantConfig]`,
  async/Twisted-safe. `get_current_tenant()` is the single source of
  truth used everywhere downstream.

### Configuration

- `synapse/config/tenants.py` — `TenantConfig` dataclass, YAML schema
  validation, per-tenant `server_name`, `database_schema`,
  `signing_key_path`, `media_store_path`, `registration_enabled`,
  `federation_enabled`.

### Database isolation

- One PostgreSQL **schema per tenant**, shared connection pool.
- `synapse/storage/database.py` issues `SET search_path TO
  <tenant_schema>, public` at transaction start. Existing Synapse SQL
  is unchanged.
- `scripts/create_tenant_schema.py` bootstraps a tenant schema from a
  template.

### Signing keys

- `synapse/crypto/multitenant_keyring.py` — `MultiTenantKeyring` holds
  one Ed25519 key per tenant.
- `/_matrix/key/v2/server` returns the key matching the requested
  `server_name`.

### Media filesystem layout

- `synapse/media/multitenant_filepath.py` — rewrites every media path
  under the active tenant's `media_store_path`.

### Observability & per-request audit (phase 1, mostly done)

**Instrumentation, already in the fork before this audit:**

- Per-request `LoggingContext` carries `server_name=effective_server_name`,
  where `effective_server_name` is the active tenant's `server_name`
  (`synapse/http/site.py`). Synapse's auto-installed
  `LoggingContextFilter` exposes this as a `server_name` field on
  every log record.
- `requests_counter` and the dozens of other upstream metrics that
  use the existing `SERVER_NAME_LABEL` ("server_name") get the
  active tenant's value automatically — no parallel `tenant=` label
  was introduced, which keeps cardinality and label semantics
  aligned with upstream Synapse.

**Phase-1 deltas landed on 2026-04-07:**

- `docker-multitenant/` rig: Prometheus container scraping
  `/_synapse/metrics`, file log handler under
  `data/logs/synapse.log`, `OBSERVABILITY` and `PHASE 1 LEAK PROBES`
  test blocks in `scripts/test_tenants.py`, hot-iteration bind
  mounts so a Python edit + `docker-compose up -d synapse` is the
  full inner loop.
- `%(server_name)s` added to `docker-multitenant/config/log.config`
  so the field actually appears in the formatted output.
- `HomeServer.effective_server_name()` helper added in
  `synapse/server.py`. Returns the active tenant's `server_name` if
  a tenant context is bound, otherwise `self.hostname`. This is the
  canonical accessor for any handler / response builder that
  previously read `self.hs.hostname` directly.
- **Login leak fixed.** `synapse/rest/client/login.py` (3 sites) and
  `synapse/handlers/auth.py` (2 sites) were qualifying bare
  localparts and constructing `LoginResponse.home_server` against
  `self.hs.hostname`, so a login to `acme.localhost` produced
  `@user:localhost` and missed the tenant schema → 403 "Invalid
  username or password". All five sites now go through
  `hs.effective_server_name()`.

Verified end-to-end via the docker demo on 2026-04-07:

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

**Known remaining leak (deferred to its own commit):**

- `/.well-known/matrix/client` builder reads `public_baseurl` from
  the global config, so every tenant gets the same announced base
  URL. Closing this requires adding `public_baseurl` (and possibly
  `identity_server`) to `TenantConfig`, plus auditing every other
  reader of `hs.config.server.public_baseurl` (password reset email
  links, registration emails, identity server discovery). Sized as
  a small standalone task; tracked by `test_wellknown_client_per_tenant`
  in the leak-probe suite.

**Other audit work still belonging to phase 1 but blocked by phase 2:**

- Tenant labelling on background processes (push, retention, stats,
  user-directory) — they currently run outside any request
  `LoggingContext`, so their log lines fall back to
  `server_name=unknown_server_from_sentinel_context`. This naturally
  rolls into phase 2 (tenant-aware background processes).

### Tenant-aware background processes (phase 2, in progress)

**Foundation landed on 2026-04-07:**

- `synapse/tenant_background.py` — `run_as_background_process_per_tenant(desc, hs, func, …)`.
  Iterates the registry, binds `tenant_context()` *inside* each scheduled
  coroutine (so the ContextVar survives Twisted's `ensureDeferred`
  boundary), schedules one `run_as_background_process` per tenant, and
  falls back to a single-process call labelled with `hs.hostname` when
  multi-tenant mode is disabled. This is the DRY helper every subsequent
  bg-process conversion goes through.
- `synapse/server.py` — `hs.get_tenant_registry()` cached singleton,
  backed by `create_tenant_registry(self)`. Prior to this the registry
  was only reachable via `SynapseSite`; background processes now have a
  clean access path that matches how every other DI-managed component
  is exposed.

**Converted loops:**

- `synapse/handlers/user_directory.py` — `notify_new_event` now fans
  out through the helper. `pos` and `_is_processing` are keyed per
  tenant (they were per-instance global state that would collide
  across tenants). `_unsafe_process` reads its
  `effective_server_name` for `Measure` blocks and
  `event_processing_positions` metric labels from `get_current_tenant()`
  instead of the constructor-time `self.server_name`.
- `synapse/handlers/stats.py` — same pattern: per-tenant `pos` and
  `_is_processing` dicts, `event_processing_positions{name="stats"}`
  now labelled with the per-tenant `effective_server_name`.
- `synapse/handlers/pagination.py` — the retention purge
  `looping_call` now goes through `run_as_background_process_per_tenant`,
  so the purge loop runs once per tenant with the tenant's own
  `search_path` bound. Verified in logs as
  `[server_name=acme.localhost] synapse.handlers.pagination … [purge] …`
  on a 3 s test interval.
- `synapse/handlers/account_validity.py` — the `send_renewals`
  looping_call (email-based renewal sweeps) is routed through the
  helper. The `@wrap_as_background_process` decorator on the inner
  method was dropped in favour of the per-tenant fan-out so each
  tenant's user table is swept with the correct schema bound.
- `synapse/handlers/deactivate_account.py` — the startup
  `_start_user_parting` resume hook now fans out via
  `run_as_background_process_per_tenant`, and the in-process "already
  running" flag was converted to a per-tenant set
  (`_user_parter_running_tenants`) so two tenants can part users
  concurrently without blocking each other. The request-path entry
  point (`deactivate_account` → `_start_user_parting`) relies on the
  now-tenant-aware `hs.run_as_background_process` to carry the caller's
  tenant through the background spawn.

Verified end-to-end via the docker demo on 2026-04-07 (new
`PHASE 2 PROBES` block in `docker-multitenant/scripts/test_tenants.py`):

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
           (background_process_start_count {name='user_parter_loop'}
            present for all tenants)

**Remaining phase 2 conversions:**

- `synapse/handlers/user_directory.py:614` —
  `kick_off_remote_profile_refresh_process` (startup + periodic
  refresh). Deferred because the inner
  `kick_off_remote_profile_refresh_process_for_remote_server` takes a
  *remote* server_name argument, so the per-tenant fan-out semantics
  need thinking through (each tenant has its own view of "remote").
  Belongs in the federation phase anyway.
- `synapse/handlers/presence.py` — presence update / timeout loops.
- `synapse/push/pusherpool.py`, `synapse/push/emailpusher.py`,
  `synapse/push/httppusher.py` — per-user pushers, scheduling loop
  currently global. Needs care: pushers belong to a specific user,
  and the user already pins the tenant, so the fan-out may want a
  different shape than the `notify_new_event`-style loops.
- Storage-layer background updaters (`synapse/storage/database.py`,
  `synapse/storage/background_updates.py`) — deliberately *not*
  converting these in phase 2: they operate on the whole DB, not
  on per-tenant rows, and the schema bootstrap step already runs
  them against each tenant schema.

### Operator surface

- `synapse/rest/admin/tenants.py` — `/_synapse/admin/v1/tenants[…]`
  endpoints (list, status, reload keys).
- `scripts/synapse_tenant` CLI (`list`, `validate`, `create`,
  `init-schema`, `generate-key`).
- `docker-multitenant/` reference deployment with nginx + Postgres +
  Synapse for `acme.localhost`, `corp.localhost`, `startup.localhost`.

---

## 🚧 Gaps before "full" multi-tenancy

What works today is the **request lifecycle** and **storage isolation**
for the synchronous Matrix Client-Server API. The asynchronous,
out-of-band, and cross-server features below still assume a single
homeserver and need to become tenant-aware.

### 1. File storage 📦

The current implementation isolates the **path** (`media/<tenant>/…`)
but assumes a single local filesystem. A real deployment needs more.

- **Storage providers per tenant.** Synapse's pluggable
  `media_storage_providers` (S3, Swift, local) is configured globally.
  Each tenant should be able to declare its own backend (e.g. tenant
  `acme` → S3 bucket `acme-media`, tenant `corp` → on-prem MinIO).
  Requires extending `MediaRepository` and the storage-provider
  interface to thread the active `TenantConfig` through every
  `store_file` / `fetch_file` call.
- **Quotas and lifecycle.** Per-tenant storage quotas, retention
  policies, and background purge jobs (`media_retention`) are
  homeserver-wide today.
- **Thumbnailer / URL-preview cache.** The thumbnail and URL-preview
  caches are global on disk and not keyed by tenant — fixable by
  routing them through `multitenant_filepath`, but currently they
  cross the boundary.
- **Media repository worker.** When deployed as a dedicated worker
  (`synapse.app.media_repository`), the worker has no knowledge of
  the per-request tenant context — it needs the same routing layer
  as the main process.
- **Backup / restore per tenant.** Today the only option is a global
  pg_dump + tarball of the media root.

### 2. Federation 🌐

Federation is the largest remaining gap. Today the fork answers
`/_matrix/key/v2/server` per tenant, but everything past key exchange
still behaves as one homeserver.

- **Outbound federation queues.** `FederationSender` maintains a single
  per-destination queue keyed only by remote server name. With N
  tenants talking to the same remote, transactions get co-mingled and
  signed with whichever key the global `Keyring` returns. Needs:
  - Per-tenant `FederationSender` instances (or a tenant-keyed queue),
  - Transaction signing using `MultiTenantKeyring.get_signing_key(tenant.server_name)`,
  - Per-tenant retry/back-off state in the database.
- **Inbound federation routing.** `/_matrix/federation/v1/*` requests
  arrive with an `Authorization: X-Matrix origin=…,destination=…`
  header. Tenant resolution must be done from the **destination**
  field, not the `Host` header (federation traffic typically hits a
  single SRV-resolved endpoint). The `TenantRouter` needs a
  federation-aware code path.
- **Server discovery (`.well-known/matrix/server`).** Each tenant needs
  its own `.well-known` document served from its own apex. Currently
  this is an nginx concern and isn't part of the fork.
- **Federation allow-lists / blocklists.** `federation_domain_whitelist`
  and similar settings are global; should move into `TenantConfig`.
- **EDU routing** (typing, presence, receipts, to-device). Same
  per-tenant queue problem as PDU sending.
- **Federation workers.** `synapse.app.federation_sender` and
  `federation_reader` workers need full tenant context propagation.
- **Server ACLs and event auth.** Room-version event auth uses
  `hs.hostname` in a few places — those reads need to become
  `get_current_tenant().server_name`.

### 3. End-to-end encryption (chiffrement) 🔐

E2EE in Matrix is mostly client-side, but the homeserver still
mediates a non-trivial amount of key material. None of it is currently
tenant-scoped beyond the database row level.

- **Device keys & one-time keys.** Stored in the per-tenant schema
  already (✓), but the upload/claim handlers
  (`/_matrix/client/v3/keys/{upload,claim,query}`) need an audit pass
  to confirm no global cache leaks across tenants
  (`e2e_keys_handler`, `_get_e2e_device_keys_for_federation_query`).
- **Cross-signing keys.** Master / self-signing / user-signing key
  storage and signature upload paths need the same audit. The
  in-memory signature verification cache is global.
- **Key backup (SSSS).** `/_matrix/client/v3/room_keys/*` writes to
  the per-tenant schema (✓), but version metadata caches and the
  background key-backup garbage collector are not tenant-aware.
- **`/_matrix/key/v2/query` (federation key fetch).** When tenant A
  fetches a remote server's signing keys, the result is cached in a
  global table and reused by tenant B. This is functionally fine
  (the remote server's keys are public) but should be schema-scoped
  for clean isolation and per-tenant trust roots.
- **To-device messages.** Per-tenant routing inside the process is
  fine; the federation side inherits the federation gaps above.
- **Per-tenant signing-key rotation.** `MultiTenantKeyring` exposes a
  reload endpoint, but there is no tooling for graceful rotation
  (publishing the new key, draining the old key's caches, updating
  `old_verify_keys`).

### 4. Other things needed for "full" multi-tenancy

Smaller but real gaps:

- **Hot add/remove of tenants.** Currently a config reload + process
  restart. The registry, DB pool, keyring, and media layout all need
  reload hooks.
- **Per-tenant workers.** No worker type is tenant-aware. Long-term
  we either need tenant-keyed workers, or sticky routing so a given
  tenant's traffic always lands on the same worker.
- **Background processes.** Push, presence, account-validity emails,
  user-directory rebuilds, stats, retention purges — all run as
  global loops and need to iterate per tenant.
- **Application services.** `app_service_config_files` is global.
  Each tenant should be able to register its own bridges/bots.
- **SSO / OIDC / CAS / SAML.** Identity provider config is global.
  Should live in `TenantConfig`.
- **Push gateways & email.** SMTP and push notification config
  (`email`, `push`, Sygnal endpoints) is global.
- **Rate limiting.** Limiters are keyed by user/IP, not tenant.
  Noisy-neighbor protection needs per-tenant ceilings.
- **Metrics & logging.** Prometheus metrics aren't labelled by
  tenant; structured logs don't carry the tenant either. Both are
  cheap wins given the `ContextVar` already exists.
- **Backup / restore per tenant.** Schema-level `pg_dump` is doable
  manually but not tooled. Combined with per-tenant media backups,
  this should become a `synapse_tenant backup` / `restore` command.
- **Account data & presence isolation.** Audit pending for any
  module that reads `hs.hostname` directly instead of going through
  the tenant context.

---

## Suggested phasing

Ordered by **what unblocks the next iteration of work**, not by
production blast radius. The local docker demo can already exercise
the synchronous Client-Server API for several tenants, so the goal
of the early phases is to make *that* path correct and observable
before adding the production-only pieces (S3, federation, workers).

1. **Audit & instrumentation.** *Mostly done — see the
   "Observability & per-request audit" entry under Implemented for
   the full landed-state writeup.* The fork already had the per-request
   instrumentation in place; phase 1 added the visibility (log
   format, Prometheus rig), the test harness, the
   `HomeServer.effective_server_name()` helper, and fixed the first
   real leak surfaced by the probes (login → wrong server_name in
   user qualification).

   **Remaining before phase 1 can be declared closed:**
   - `/.well-known/matrix/client` builder leak — needs `public_baseurl`
     on `TenantConfig`. Small standalone task. Failing probe:
     `test_wellknown_client_per_tenant`.
   - 2-3 more leak probes worth adding (registration response,
     password-reset email link, `/_matrix/federation/v1/version`)
     to flush out adjacent leaks before declaring the audit done.

   The background-process audit (push, retention, stats — log lines
   fall back to `server_name=unknown_server_from_sentinel_context`)
   overlaps with phase 2 and is tackled there.
2. **Background processes made tenant-aware.** *In progress — see the
   "Tenant-aware background processes" entry under Implemented for the
   landed pieces.* The foundational helper
   (`run_as_background_process_per_tenant`) and the
   `hs.get_tenant_registry()` access path are in, and the first two
   highest-leverage loops are converted and gated by green probes in
   the docker rig: `user_directory`, `stats`, `pagination` (retention
   purge), `account_validity`, and `deactivate_account` (user parter).
   Remaining conversions: presence, pushers, and user_directory's
   remote-profile-refresh sub-loop (deferred to the federation phase
   because it takes a remote server_name argument).
3. **Per-tenant SSO / email / push / identity config.** Move
   `oidc_providers`, `cas_config`, `saml2_config`, `email`, `push`,
   and Sygnal endpoints into `TenantConfig`. Unblocks real tenant
   onboarding (registration with email verification, password reset,
   login via IdP all hit this immediately).
4. **Per-tenant rate limiting + app services.** Tenant-keyed
   limiters for noisy-neighbor protection; per-tenant
   `app_service_config_files` so each tenant can register its own
   bridges and bots.
5. **File storage providers.** Extend the storage-provider
   interface to take a `TenantConfig`; ship an S3-per-tenant
   implementation; route the thumbnailer / URL-preview cache
   through `multitenant_filepath`. Unblocks any deployment beyond
   a single host (k8s pods don't share disks, restarts lose local
   media — it's optional for the docker demo, table stakes for
   anything real).
6. **Hot add/remove + backup/restore tooling.** Reload hooks for
   the registry, DB pool, keyring, and media layout;
   `synapse_tenant backup` / `restore` commands wrapping
   schema-level `pg_dump` plus the per-tenant media tree.
7. **Dynamic tenant control plane.** Database-driven tenancy
   replaces YAML tenant list. Standalone control plane service
   handles full tenant lifecycle (create, suspend, activate,
   delete). AES-256-GCM encrypted signing keys in DB. Synapse
   boots with zero tenants and loads from `public.tenants` table.
8. **Database tuning & connection optimization.** Raise the tenant
   ceiling from ~10-15 to ~100-200 before tackling federation.
   Three sub-phases, each independently shippable:

   **8a — `SET LOCAL` search_path.** Replace the current 3-round-trip
   pattern (`SHOW search_path` → `SET search_path` → `SET` restore)
   with `SET LOCAL search_path TO <schema>`, which scopes to the
   transaction and auto-resets on commit/rollback. Eliminates
   `_restore_search_path` entirely. ~10 lines in
   `synapse/storage/database.py`. Impact: ~33% fewer DB round trips
   per transaction.

   **8b — Connection-level schema caching.** Track which schema each
   connection is currently set to (`dict[connection_id, schema_name]`).
   Before issuing `SET LOCAL`, check if the connection already has the
   right schema — if so, skip the SET. Most requests within a burst
   hit the same tenant, so this eliminates SET overhead for consecutive
   same-tenant requests. ~50 lines in `database.py`.

   **8c — Connection pool tuning + pgbouncer.** Raise `cp_max`
   guidance from 10 to 50-100 in docker-demo configs and document the
   recommendation. Add a pgbouncer sidecar to
   `docker-demo/docker-compose.yml` with `search_path` set at
   session-assign time. Document the capacity curve in
   `docs/multi_tenant.md`.

   **Verification:** Stress test (`docker-demo/stress-test/`) before
   and after each sub-phase, measuring requests/sec, p95 latency, and
   pool utilization at 5, 10, and 20 tenant load. See
   `challenges.md` for the full bottleneck analysis and capacity
   estimates backing this phase.
9. **Federation outbound.** Per-tenant `FederationSender` (or a
   tenant-keyed queue), transactions signed via
   `MultiTenantKeyring.get_signing_key(tenant.server_name)`,
   per-tenant retry/back-off state. Unblocks talking to the public
   Matrix network from more than one tenant.
10. **Federation inbound.** Destination-header routing in
    `TenantRouter` (federation traffic doesn't carry the right
    `Host`), per-tenant `.well-known/matrix/server`, per-tenant
    federation allow-lists, EDU routing.
11. **E2EE audit pass.** Verify no device-key, cross-signing, or
    key-backup cache crosses tenants; tenant-scope the federation
    `/_matrix/key/v2/query` cache; build graceful signing-key
    rotation tooling.
12. **Workers.** Tenant-aware `federation_sender`,
    `media_repository`, and `pusher` workers — sticky routing or
    tenant-keyed instances. Only matters at scale, so it lands
    last.

Each phase is independently shippable and leaves the fork in a
working state for the tenants it already serves.
