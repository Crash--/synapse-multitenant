# Phase 6 kickoff prompt

Paste this into a fresh Claude Code session on the `feature/multi-tenant` branch.

---

## Prompt

You are continuing multi-tenant Synapse work on the `feature/multi-tenant` branch at `/home/monta/Documents/workspace/synapse-multitenant`.

**Read these first (narrow slices, not full files):**
1. `CLAUDE.md` — project rules, token discipline, subagent commit hygiene, test environment, where-to-look table
2. `docs/multi_tenant_roadmap.md` lines 410-413 — phase 6 scope
3. `roadmap-progess.md` — executive summary (phases 1-5 are ✅, phase 6 is ⏸)
4. `docs/superpowers/plans/2026-04-09-phase-5-file-storage-providers.md` lines 1-15 — structural template for the plan doc

**Phase 5 is ✅ Complete.** 8-probe suite. `FileStorageProviderBackend._tenant_base` + `MediaStorage._local_path` (4 join sites replaced), `MediaRepository` wired to `MultiTenantMediaFilePaths` via factory, URL previewer + thumbnailer audited clean. S3 deferred. Branch is clean except tracker files (uncommitted by design).

**Phase 6 — Hot add/remove + backup/restore tooling.**

This is the operational phase. Without it, adding a new tenant means restarting the entire Synapse process (downtime for all tenants), and there's no way to back up or restore a single tenant's data without a full database dump.

### Scope from the roadmap

Reload hooks for the registry, DB pool, keyring, and media layout; `synapse_tenant backup` / `restore` commands wrapping schema-level `pg_dump` plus the per-tenant media tree.

### What I know about the codebase shape

**SIGHUP reload infrastructure (already exists):**
- `synapse/app/_base.py:115` — `register_sighup(hs, func, *args, **kwargs)` registers a callback to be invoked when the process receives SIGHUP. Callbacks run on the next reactor tick via `callFromThread`.
- `synapse/app/_base.py:607` — `setup_sighup_handling()` wires the signal handler. Already called at startup.
- Existing SIGHUP consumers: log config reload, TLS certificate reload. The tenant subsystems do not register any SIGHUP hooks.

**Singletons that cache tenant state at startup:**
- `synapse/server.py:710` — `get_tenant_registry()` — returns a `TenantRegistry` (constructed from `MultiTenantConfig`). Uses `@cache_in_self`, so it's built once and cached.
- `synapse/server.py:723` — `get_tenant_ratelimiter_registry()` — `TenantRatelimiterRegistry`, also `@cache_in_self`.
- `synapse/server.py:757` — `get_tenant_app_service_registry()` — `TenantAppServiceRegistry`, also `@cache_in_self`.
- `synapse/server.py:1111` — `get_multi_tenant_keyring()` — `MultiTenantKeyring`, also `@cache_in_self`.
- `synapse/media/media_repository.py:108-152` — `MediaRepository.__init__` constructs `filepaths` and `media_storage` once at init from global config.

**`@cache_in_self` (synapse/server.py:229):**
The decorator caches the return value of `get_*` methods on the HomeServer instance. There's no built-in invalidation — once called, the cached value persists for the process lifetime. To support hot-reload, we either need to (a) add an invalidation mechanism to `cache_in_self`, (b) bypass it for the registries that need reloading, or (c) make the registries themselves mutable (they re-read config on a `reload()` call).

**TenantRegistry (synapse/tenant_registry.py:51):**
- `__init__` takes a `MultiTenantConfig`, stores `self._tenants = multi_tenant_config.tenants`. Immutable dict after init.
- No `reload()` or `add_tenant()` method exists.
- Used by: keyring, app service registry, rate limiter registry, HTTP site routing, admin endpoints.

**MultiTenantKeyring (synapse/crypto/multitenant_keyring.py:42):**
- `__init__` takes `(hs, tenant_registry)`, loads signing keys from disk for each tenant. No reload method.

**Tenant CLI (`scripts/synapse_tenant`):**
- Existing subcommands: `list`, `create`, `validate`, `generate-key`, `init-schema`.
- No `backup` or `restore` subcommands.
- `create` adds a tenant to the YAML config and runs `init-schema`. It does NOT signal the running process — a restart is required.

**Schema management (`scripts/create_tenant_schema.py`):**
- Creates PostgreSQL schemas and clones tables/sequences from a template. Used by `synapse_tenant init-schema`.

### Task

Use the `/brainstorm` skill first, then the `/write-plan` skill to produce a plan doc at `docs/superpowers/plans/2026-04-09-phase-6-hot-add-remove-backup-restore.md`. Follow the phase-5 plan structure (context → decomposition → sub-phases → task lists → definition of done → risks).

Key design decisions to resolve in brainstorming:
1. **Hot-reload mechanism.** SIGHUP is the natural trigger (Synapse already uses it for log/TLS reload). On SIGHUP: (a) re-read `homeserver.yaml` tenant block, (b) diff against current `TenantRegistry`, (c) for new tenants: create schema if needed, load signing key, add to registry + derived registries, (d) for removed tenants: mark inactive (reject new requests) but don't drop schema. Key question: should `TenantRegistry` become mutable (with a `reload()` method), or should we replace the cached instance entirely?
2. **`cache_in_self` invalidation.** The registries (`TenantRatelimiterRegistry`, `TenantAppServiceRegistry`, `MultiTenantKeyring`) are cached via `@cache_in_self`. Options: (a) add a `_invalidate_cache(name)` method to `HomeServer`, (b) make registries mutable with `reload()` methods so the cached instance updates in-place, (c) bypass `cache_in_self` for tenant registries. Option (b) is cleanest — the cached reference stays valid, the registry's internal state updates.
3. **Backup/restore scope.** A tenant backup = (a) `pg_dump` of the tenant's PostgreSQL schema + (b) tar of the tenant's media tree under `<media_store_path>/<server_name>/`. Restore = reverse. Should this be a subcommand of `synapse_tenant` (keeping all tenant ops in one CLI), or a separate script? The existing CLI pattern suggests `synapse_tenant backup --server-name acme.com` / `synapse_tenant restore --server-name acme.com --from /path/to/backup.tar.gz`.
4. **Tenant removal safety.** Removing a tenant from config should NOT drop the schema or delete media. It should only stop routing requests to that tenant. A separate `synapse_tenant drop --server-name acme.com --confirm-destructive` command can handle actual cleanup.
5. **Decomposition.** Natural axis: 6a = hot-reload (SIGHUP + registry reload + derived registries), 6b = backup/restore CLI. They're independent — 6a is runtime, 6b is offline tooling.

Constraints:
- Token discipline (see CLAUDE.md § "Token discipline")
- Subagent commit hygiene (see CLAUDE.md § "Subagent commit hygiene")
- Test environment (see CLAUDE.md § "Test environment")
- Probes-first: Task 0 of every sub-phase writes red probes
- Execute recommended choices autonomously (see memory)
- Run `/sync-roadmap` after the plan is approved and after each sub-phase ships
