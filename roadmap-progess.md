# Multi-Tenant Synapse — Roadmap Progress Report

**Date:** 2026-04-08
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
| **1. Audit & instrumentation** | ✅ Substantially done | One known leak still open (`/.well-known/matrix/client`); 2–3 more probes recommended before declaring it closed. |
| **2. Background processes made tenant-aware** | 🟢 Majority complete | 7 looping/background sites converted via the shared helper, all green in the docker probe rig. Presence and pushers deferred. |
| **3. Per-tenant SSO / email / push / identity** | ⏸ Not started | Still global config. Blocks real onboarding. |
| **4. Per-tenant rate limiting + app services** | ⏸ Not started | |
| **5. File storage providers** | ⏸ Not started | Path layout already per-tenant; storage-provider interface still global. |
| **6. Hot add/remove + backup/restore** | ⏸ Not started | |
| **7. Federation outbound** | ⏸ Not started | |
| **8. Federation inbound** | ⏸ Not started | |
| **9. E2EE audit pass** | ⏸ Not started | |
| **10. Workers** | ⏸ Not started | |

In addition to the roadmap-listed phases, two pieces of infrastructure
landed that are *not* phases of their own but are listed here because
management cares about them:

- A **second self-contained docker rig** (`docker-demo/`) intended for
  Element-driven manual demos with two real-looking domain names
  behind Traefik, with optional `mkcert` HTTPS.
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

## Suggested next steps (for management to weigh)

Ordered by what's cheapest to ship and what unblocks the most
downstream work.

1. **Close phase 1.** Land per-tenant `public_baseurl`, fix the
   `/.well-known/matrix/client` leak, add the recommended extra
   probes, and back-apply the schema-cloning fix to
   `docker-multitenant/` and `scripts/create_tenant_schema.py`. Small,
   self-contained, and removes a real correctness gap.
2. **Finish phase 2.** Convert presence and pushers (the only two
   non-deferred loops left). The remote-profile-refresh sub-loop
   stays deferred to the federation phase by design.
3. **Start phase 3 (per-tenant SSO / email / push / identity).** This
   is the first phase that unblocks *real* tenant onboarding —
   without it, registration with email verification, password reset,
   and IdP login all behave the same for every tenant. It's also a
   prerequisite for any externally-facing pilot.
4. **Defer phases 4–10 as currently sequenced.** Nothing learned in
   phases 1–2 suggests a re-order. The federation work (phases 7–8)
   remains the largest single chunk and should not be started until
   phases 3 and 5 (file storage providers) are done, because both
   feed into how a "real" tenant is provisioned.

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
