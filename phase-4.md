# Phase 4 kickoff prompt

Paste this into a fresh Claude Code session on the `feature/multi-tenant` branch.

---

## Prompt

You are continuing multi-tenant Synapse work on the `feature/multi-tenant` branch at `/home/monta/Documents/workspace/synapse-multitenant`.

**Read these first (narrow slices, not full files):**
1. `CLAUDE.md` — project rules, token discipline, subagent commit hygiene, test environment, where-to-look table
2. `docs/multi_tenant_roadmap.md` lines 399-402 — phase 4 scope
3. `roadmap-progess.md` — executive summary (phases 1-3 are ✅, phase 4 is ⏸)
4. `docs/superpowers/plans/2026-04-09-phase-3-per-tenant-sso-email-push.md` lines 1-30 — structural template for the plan doc

**Phase 3 is ✅ Complete.** 21/0 probe suite. All config dataclasses (email, OIDC, CAS, SAML, push) added to `TenantConfig`, handlers converted to lazy resolution from tenant context, server_notices MXID sweep done. Branch is clean except tracker files (uncommitted by design).

**Phase 4 — Per-tenant rate limiting + app services.**

This is the noisy-neighbor protection phase. Without it, one tenant's users can exhaust rate limits that affect all tenants, and app services (bridges, bots) are shared globally.

### Scope from the roadmap

Tenant-keyed rate limiters for noisy-neighbor protection; per-tenant `app_service_config_files` so each tenant can register its own bridges and bots.

### What I know about the codebase shape

**Rate limiting:**
- `synapse/config/ratelimiting.py` — `RatelimitSettings` (frozen attrs, `per_second` + `burst_count`), `FederationRatelimitSettings`, `RatelimitConfig` (reads ~15 `rc_*` keys from YAML)
- `synapse/api/ratelimiting.py:40` — `Ratelimiter` class (leaky bucket, takes `RatelimitSettings` at `__init__`). Per-key tracking, but keys are user IDs or IP addresses — not tenant-scoped.
- `synapse/api/ratelimiting.py:392` — `RequestRatelimiter` constructed once at startup, holds ~10 `Ratelimiter` instances (login, register, joins, invites, 3pid, etc.)
- Handlers create their own `Ratelimiter` instances at `__init__` time from `hs.config.ratelimiting.*`:
  - `synapse/handlers/room_member.py:137-175` — 5 limiters (joins_local, joins_remote, joins_per_room, invites_per_room, invites_per_user)
  - `synapse/rest/client/login.py:126-131` — 2 limiters (login_address, login_account)
  - `synapse/rest/client/register.py:407` — registration_token_validity
  - `synapse/rest/client/sync.py:138`, `synapse/rest/client/presence.py:58` — presence limiters
  - Plus ~5 more in identity, devicemessage, user_directory, media

**App services:**
- `synapse/config/appservice.py:42` — `self.app_service_config_files = config.get("app_service_config_files", [])` — global list of AS YAML files
- `synapse/appservice/api.py:129` — `self.config = hs.config.appservice`
- App services are loaded once at startup and held in a global registry

### Task

Use the `/brainstorm` skill first, then the `/write-plan` skill to produce a plan doc at `docs/superpowers/plans/2026-04-09-phase-4-per-tenant-rate-limiting-appservices.md`. Follow the phase-3 plan structure (context → decomposition → sub-phases → task lists → definition of done → risks).

Key design decisions to resolve in brainstorming:
1. **Rate limiter scoping model.** `Ratelimiter` keys are currently `(user_id,)` or `(ip_address,)`. To isolate tenants, should we (a) prefix keys with `server_name` so each tenant gets its own bucket, (b) create per-tenant `Ratelimiter` instances with different `RatelimitSettings`, or (c) something else? Option (a) is minimal change; option (b) allows different rate limits per tenant.
2. **Config shape.** Should `TenantConfig` carry a full `TenantRatelimitConfig` block (like phase 3's email/push), or is a simpler `rate_limit_scale_factor: float` per tenant enough for noisy-neighbor protection? The full block is more flexible but duplicates ~15 config keys.
3. **App service isolation model.** Each tenant having its own `app_service_config_files` list means the global AS registry must be tenant-scoped. How does this interact with the AS transaction push loop (which is a background process)? Does it need `run_as_background_process_per_tenant`?
4. **Decomposition axis.** Per-subsystem (4a=rate limiting, 4b=app services) seems natural since they're independent. Confirm or propose alternative.

Constraints:
- Token discipline (see CLAUDE.md § "Token discipline")
- Subagent commit hygiene (see CLAUDE.md § "Subagent commit hygiene")
- Test environment (see CLAUDE.md § "Test environment")
- Probes-first: Task 0 of every sub-phase writes red probes
- Execute recommended choices autonomously (see memory)
- Run `/sync-roadmap` after the plan is approved and after each sub-phase ships
