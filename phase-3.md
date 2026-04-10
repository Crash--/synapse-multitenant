# Phase 3 kickoff prompt

Paste this into a fresh Claude Code session on the `feature/multi-tenant` branch.

---

## Prompt

You are continuing multi-tenant Synapse work on the `feature/multi-tenant` branch at `/home/monta/Documents/workspace/synapse-multitenant`.

**Read these first (narrow slices, not full files):**
1. `CLAUDE.md` — project rules, token discipline, where-to-look table
2. `docs/multi_tenant_roadmap.md` lines 394-398 — phase 3 scope
3. `roadmap-progess.md` — executive summary (phase 2 is ✅, phase 3 is ⏸)
4. `docs/superpowers/plans/2026-04-08-phase-2-close-tenant-aware-bg-processes.md` lines 1-30 — structural template for the plan doc

**Phase 2 is ✅ Complete.** 34/0 probe suite. All bg loops converted, seed rows seeded, state_group caches tenant-keyed, server_notices sender MXID per-tenant. Branch is clean except tracker files (uncommitted by design).

**Phase 3 — Per-tenant SSO / email / push / identity config.**

This is the first phase that unblocks real tenant onboarding. Without it, registration with email verification, password reset, and IdP login all behave identically for every tenant. Pushers (deferred from phase 2) are included.

### Scope from the roadmap

Move `oidc_providers`, `cas_config`, `saml2_config`, `email`, `push`, and Sygnal endpoints into `TenantConfig` (or per-tenant overrides that fall back to global). Unblocks:
- Per-tenant OIDC / SSO login (each tenant talks to its own IdP)
- Per-tenant SMTP (each tenant sends email from its own domain)
- Per-tenant push notification config
- Per-tenant identity server (field already exists on `TenantConfig` from phase 1)

### What I know about the codebase shape

Config files that need per-tenant overrides:
- `synapse/config/oidc.py` — `oidc_providers` list, `oidc_callback_url`
- `synapse/config/cas.py` — `cas_config`
- `synapse/config/saml2.py` — `saml2_config`
- `synapse/config/emailconfig.py` — SMTP host/port/from, notif templates
- `synapse/config/push.py` — push group settings, jitter
- `synapse/config/account_validity.py` — renewal email config

Handler files that read config at `__init__` time (the phase-1/2 pattern: capture at init → stale under multi-tenant):
- `synapse/handlers/oidc.py:127` — `hs.config.oidc.oidc_providers`
- `synapse/handlers/oidc.py:445` — `self._server_name = hs.config.server.server_name`
- `synapse/handlers/cas.py` — `hs.config.cas`
- `synapse/handlers/saml.py` — `hs.config.saml2`
- `synapse/push/emailpusher.py:86` — `self.hs.config.email.notif_delay_before_mail_ms`
- `synapse/push/httppusher.py:126,130,514` — `hs.config.push.*`
- `synapse/push/pusherpool.py` — global pusher scheduling loop

Auth-side server_notices reads deferred from phase 2c-B (follow-up sweep):
- `synapse/api/auth_blocking.py:40`
- `synapse/handlers/room_member.py:129`
- `synapse/handlers/message.py:848-849`
- `synapse/handlers/register.py:130`
- `synapse/handlers/federation.py:147`
- `synapse/handlers/room.py:191`

### Task

Use the `/brainstorm` skill first, then the `/write-plan` skill to produce a plan doc at `docs/superpowers/plans/2026-04-09-phase-3-per-tenant-sso-email-push.md`. Follow the phase-2-close plan structure (context → decomposition → sub-phases → task lists → definition of done → risks).

Key design decisions to resolve in brainstorming:
1. **Config inheritance model.** Should each `TenantConfig` carry full SSO/email/push config blocks, or should tenants inherit from global config with per-field overrides? The `public_baseurl` / `identity_server` / `server_notices_mxid` pattern from phases 1-2 used individual fields with `effective_*` accessors — does that scale to OIDC provider lists?
2. **Decomposition axis.** Per-subsystem (3a=OIDC, 3b=email, 3c=push) vs per-layer (3a=config fields, 3b=handler conversion, 3c=probes)? Phase 2 used severity-first; phase 3 has no obvious severity gradient.
3. **Pusher fan-out shape.** Pushers belong to a user who pins the tenant, so they don't need the `run_as_background_process_per_tenant` pattern. But `PusherPool` is a singleton that dispatches globally. What's the minimal change?
4. **OIDC callback URL.** Under multi-tenant, the callback URL must be per-tenant (the IdP redirects back to the tenant's domain). How does this interact with nginx routing?

Constraints:
- Token discipline (see CLAUDE.md § "Token discipline")
- Probes-first: Task 0 of every sub-phase writes red probes
- Execute recommended choices autonomously (see memory)
- Run `/sync-roadmap` after the plan is approved and after each sub-phase ships
