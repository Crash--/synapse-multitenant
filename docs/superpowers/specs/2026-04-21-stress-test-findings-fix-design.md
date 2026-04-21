# Stress-Test Findings Fix (Design Spec)

**Date:** 2026-04-21
**Branch:** `feature/multi-tenant` (implementation on a dedicated worktree)
**Status:** Design — to be turned into an implementation plan.
**Companion:** `docker-demo/stress-test/results.md` (2026-04-20 run that produced these findings)

---

## Problem

The 2026-04-20 stress run against `docker-demo/` produced three defects — captured in `docker-demo/stress-test/results.md` — that need to be addressed together because they cross-amplify.

### Finding #1 — Cross-tenant token leak (security boundary violation)

A token minted for `stress-a.localhost` is accepted by `stress-b/c/d.localhost` and `/_matrix/client/v3/account/whoami` returns `@user0:stress-a.localhost` verbatim. Root cause: `get_user_by_access_token(token)` in `synapse/storage/databases/main/registration.py:467-478` is wrapped with `@cached()` whose key is `(token,)` only — no tenant dimension. First lookup populates the process-global `DeferredCache`; subsequent lookups of the same token under a different `Host` return the cached `TokenLookupResult` without ever consulting the DB, so the per-transaction `search_path` isolation is moot.

This is a security-boundary violation per `CLAUDE.md` (*"never leak data across `TenantConfig` boundaries"*).

### Finding #2 — `createRoom` state-group schema leak

`POST /_matrix/client/v3/createRoom` fails reliably with HTTP 500 (`Exception: Trying to persist state with unpersisted prev_group: 20` and several `UniqueViolation`s on `state_groups_persisting_pkey`, `state_group_edges_…_idx`, `sliding_sync_joined_rooms_event_stream_ordering_idx`). DB inspection after one failed attempt:

```
public.state_groups                               -> 17 rows
tenant_stress_a_localhost.state_groups            -> 0 rows
public.state_group_id_seq                         -> 17
tenant_stress_a_localhost.state_group_id_seq      -> 20
```

Static analysis produced three competing hypotheses (startup-time writes land in `public`; `_set_tenant_schema` omits the `, public` fallback documented in `docs/multi_tenant.md`; a process-global `state_groups_persisting` tracker isn't tenant-partitioned) and cannot pick a winner without runtime instrumentation. The instrumentation phase is therefore part of the fix.

### Finding #3 — Tenant registry empty at startup

On `docker compose up -d`, Synapse logs `Tenant registry initialized with 0 tenants: []` and serves every tenant `Host` as the default `server_name=localhost` until something hits `/_synapse/admin/v1/tenants/reload`. Root cause: when `multi_tenant.source: "database"`, the config parser at `synapse/config/tenants.py:672-679` explicitly logs *"tenants will be loaded from the public.tenants table"* but leaves `tenants_dict = {}`, and **nothing calls `load_tenants_from_database()` at startup** — it only runs from `ReloadTenantsRestServlet.on_POST` (`synapse/rest/admin/tenants.py:536-552`). SIGHUP reloads YAML only (`synapse/app/_base.py:697-726`). The contract is documented; the code path is missing.

This affects every `source: "database"` deployment, not just the demo.

### Why bundle

The three findings cross-amplify. The pre-`/reload` window opened by #3 causes traffic to `stress-a.localhost` to be served as `localhost`, writing users / tokens / state groups into `public`. Those rows then become the substrate for both #1's cache leak (token rows reachable from tenant schemas via search-path fallback) and plausibly #2's state-group pollution (startup activity + falling-through requests filling `public.state_groups`). Fixing #3 first removes the precondition; fixing #1 closes the security hole; #2 is then attacked on a clean substrate with real data from instrumentation.

## Goal

On a fresh `docker compose up -d` of `docker-demo/`:

1. Synapse logs `Tenant registry initialized with N tenants: [...]` with N = the number of rows in `public.tenants` — no out-of-band `/reload` required.
2. A token minted for tenant A is rejected (401/403) by every other tenant's endpoints, on both cache-hit and cache-miss paths.
3. `createRoom` against any provisioned tenant succeeds; state groups, edges, and persisting-tracker rows land in that tenant's schema only; `public.state_groups` does not accumulate rows from tenant activity.
4. Re-running `docker-demo/stress-test/stress-test-noroom.js` at the same profile (peak 200 VUs) yields ≥99% pass rate on `login`, `whoami`, `sync`, and **cross-tenant isolation** checks.
5. Re-running the original room-based `docker-demo/stress-test/stress-test.js` runs to completion.

## Non-goals

- Per-tenant cache instances (Finding #1's "option 1c"). Architecturally cleaner but a separate project; the tenant-aware cache key closes the bug at the descriptor layer without touching call sites.
- Hot-add / hot-remove of tenants. Still requires a restart or explicit `/reload`; this spec only ensures that a fresh boot hydrates once.
- Reworking the control-plane startup sequence. The control plane stays reactive (calls `/reload` after provisioning); the Synapse-side hook is the durable fix.
- F#2 fix details. The spec commits to instrumentation + reproduce + root-cause confirmation as prerequisites; the actual fix shape is determined by the evidence and added to the implementation plan after step 4 (see "Work order").

## Work order

Single PR on a worktree branched off `feature/multi-tenant`. Commits land in this order; each is a focused, reviewable unit.

1. **F#3 fix** — startup hydration (+ unit test).
2. **F#1 fix** — tenant-aware cache key + audit of other `@cached()` decorators in storage (+ unit test).
3. **F#2 instrumentation** — logs + env-gated assertions, committed permanently.
4. **F#2 reproduce** — run instrumented stack, capture logs, confirm root cause. This is a plan step, not a commit.
5. **F#2 fix** — scope determined by step 4 (+ unit test).
6. **End-to-end** — re-run both stress-test variants; record results in a new `docker-demo/stress-test/result-2026-04-NN.md`.

## Design — Finding #3 (registry startup hydration)

**Change summary.** Honor the contract `synapse/config/tenants.py:672-679` already documents. Move the DB-loading logic out of the REST handler into the registry, call it once during Synapse bootstrap when `multi_tenant.source == "database"`.

**Touchpoints.**

- `synapse/tenant_registry.py` — add async method `async def load_from_database(self, db_pool) -> None`. It performs the same query/transform currently in `synapse/rest/admin/tenants.py:536-552`, replaces the in-memory tenant map, and re-fires any downstream notifiers (keyring, appservice, ratelimiter, OIDC — whatever `/reload` currently cascades into).
- `synapse/rest/admin/tenants.py` — `ReloadTenantsRestServlet.on_POST` delegates to `registry.load_from_database()` instead of holding the query inline. Removes duplication.
- `synapse/app/homeserver.py` (or the module that owns `setup()` for the main process — precise location to be pinned in the plan) — after the DB pool is available and before the first request can be served, if `hs.config.multi_tenant.enabled and hs.config.multi_tenant.source == "database"`, `await hs.get_tenant_registry().load_from_database(hs.get_db_pool())`.

**Failure modes.** If the DB is unreachable at startup, log `ERROR` and boot with an empty registry (today's behavior). Do not retry — if the DB is down, nothing else works anyway, and `/reload` remains available once it comes back. Do not raise — that would regress startup in partial-failure scenarios.

**Worker processes.** For federation / event-persister workers, the registry is loaded lazily the same way (same hook); synchronization with the main process happens via the DB as the source of truth.

**Single-tenant / YAML-source deployments.** Unaffected. YAML-source parsing already populates `tenants_dict`; the new hook only runs when `source == "database"`.

**Unit test.** `tests/tenant/test_registry.py::TenantRegistryTestCase.test_load_from_database_at_startup`:

1. Build a `MultiTenantConfig` with `source="database"`, empty tenants dict.
2. Seed an in-memory SQLite (or the existing test DB) `public.tenants` with two rows.
3. Instantiate the registry, `await registry.load_from_database(db_pool)`.
4. Assert `registry.tenants` has both rows.
5. Assert the registry log line reflects the loaded count.

## Design — Finding #1 (cross-tenant token cache)

**Change summary.** Introduce a tenant-aware variant of the `@cached()` descriptor. Apply it to `get_user_by_access_token`. Audit the rest of `synapse/storage/` for other `@cached()` / `@cachedList()` decorators on methods that read tenant-scoped tables, swap each one.

**Touchpoints.**

- `synapse/util/caches/descriptors.py` — new `@tenant_cached(...)` decorator (and `@tenant_cached_list`). Under the hood, the cache key is `(tenant_id, *original_args)`, where `tenant_id = tenant_context.get_current_tenant().server_name if tenant_context.get_current_tenant() else None`. Single-tenant deployments always get `tenant_id=None` — indistinguishable from today's behavior. All other behavior (TTL, invalidation, eviction) is preserved.
- `synapse/storage/databases/main/registration.py:467-478` — swap `@cached()` → `@tenant_cached()` on `get_user_by_access_token`.
- Any other storage method the audit turns up.

**Audit scope.** The audit output is part of the spec deliverable, captured inline in the implementation plan:

1. Enumerate: `rg '@cached\(' synapse/storage/` + `rg '@cachedList\(' synapse/storage/`.
2. For each hit, classify:
   - **Swap** — method reads a table scoped per-tenant schema AND its cache key does not naturally include tenant-disambiguating data. Example: `get_user_by_access_token(token)` — bare token is ambiguous.
   - **Safe, explicit** — method's cache key already includes the tenant suffix via its arguments (e.g. `get_user_by_id("@user0:stress-a.localhost")` — the server_name suffix disambiguates). Document why.
   - **Not tenant-scoped** — method reads `public`-only metadata. Document why.
3. Apply swaps. Record the audit table in the implementation plan and in a short comment at the top of `descriptors.py` (or a new `docs/multi_tenant/caches.md` if the table is long).

**Non-obvious concerns.**

- **Invalidation propagation.** When a token is invalidated (logout, device deletion), the cache invalidation path must pass through the tenant context so the right cache key is evicted. Today, the invalidator runs in the same request context as the write, so `get_current_tenant()` returns the right tenant and the generated cache key matches. We will verify this in the unit test — invalidate in tenant A, expect lookup in tenant A to miss; lookup in tenant B (unrelated cached entries) to hit.
- **`@cachedList` keys.** The list descriptor iterates arguments; `tenant_cached_list` must prepend the tenant prefix to every materialized per-item key consistently.
- **Cache size accounting.** Existing LRU sizing is per-descriptor; with tenant in the key, worst-case unique keys multiply by `N_tenants`. Document the implication; leave sizing unchanged unless the audit reveals a concrete problem.

**Unit test.** `tests/storage/test_registration_multitenant.py::TokenCacheIsolationTestCase`:

1. Set up two tenant configs, schemas, fake token rows in each.
2. Enter tenant A context; call `store.get_user_by_access_token("T_A")`; assert result's `user_id` endsWith `:A`.
3. Switch to tenant B context (fresh contextvar); call `store.get_user_by_access_token("T_A")`.
4. Assert: either the call misses the cache and returns `None`/B's row (never A's row).
5. Switch back to tenant A; call `store.get_user_by_access_token("T_A")`; assert cache hit, same A-row.

Before the fix lands this test fails at step 4. After the fix it passes.

## Design — Finding #2 (instrumentation, then fix)

**Instrumentation (committed permanently — this is the durable guard, not scaffolding).**

- `synapse/storage/database.py::_set_tenant_schema` — DEBUG log `"txn=%s tenant=%s search_path=%s"` on every call.
- `synapse/storage/databases/state/store.py`:
  - At `build_sequence_generator` call site (constructor): INFO-log the startup-time state (no tenant context, max id read, resolved schema for `state_groups`). One-shot at boot.
  - At every `nextval('state_group_id_seq')` call: DEBUG log `"tenant=%s search_path=%s next_id=%s"`.
  - At every `INSERT INTO state_groups`, `state_group_edges`, `state_groups_persisting`: DEBUG log `"tenant=%s search_path=%s target_schema=%s id=%s"` where `target_schema` is the PG-resolved schema (`SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relname='state_groups' AND n.nspname = ANY(current_schemas(false)) LIMIT 1`).
- `SYNAPSE_MT_STRICT_STATE_GROUPS=1` env gate. When set, add an assertion `get_current_tenant() is not None` before every write to `state_groups`, `state_group_edges`, `state_groups_persisting`. Off by default; on during reproduce runs and CI.

**Reproduce protocol (plan step 4, not a code commit).**

1. `docker compose down -v && docker compose up -d` with `SYNAPSE_MT_STRICT_STATE_GROUPS=1`.
2. Provision `stress-a` via the control plane (existing path).
3. Verify registry hydration (F#3 fix in effect) — no `/reload` needed.
4. Attempt one `POST /_matrix/client/v3/createRoom` as `@user0:stress-a.localhost`.
5. Capture Synapse container logs, filter for `state_groups|search_path|tenant=|nextval`.
6. Correlate with DB inspection (`SELECT * FROM public.state_groups`, same for tenant schema, and the two `state_group_id_seq`s).
7. Write findings into a short `docker-demo/stress-test/finding-2-repro-2026-04-NN.md` — one of (A) startup-path, (B) search-path drop, (C) global tracker, with the log excerpt that proves it.

**Fix (added to the plan after step 4).** Shape depends on which hypothesis the evidence picks:

- If **(A) startup writes to public**: either defer state-group seeding until a tenant context is active, or qualify all startup-time seeding to a designated tenant (likely wrong), or convert the seeding into per-tenant bootstrap run under `with tenant_context(...)`. Most likely shape.
- If **(B) search-path drops `public`**: add `, public` to `_set_tenant_schema` (contradicts the "security-first" comment already in the code — need to reconcile; per `docs/multi_tenant.md:319-320` the intended behavior IS `<tenant>, public`). Smallest-diff fix.
- If **(C) global `state_groups_persisting` tracker**: partition the tracker by tenant (in-memory dict keyed on tenant.server_name, or a schema-qualified table write). Largest-diff fix.

Each shape has distinct testing; the plan will be updated with the concrete test once step 4 completes.

**Unit test (post-repro).** A regression test that exercises the fixed code path in a way that would have surfaced the bug pre-fix. Exact shape determined by which hypothesis wins.

## Testing strategy

- **TDD order** for F#1 and F#3: failing test first (local `trial tests.tenant.test_registry.TenantRegistryTestCase.test_load_from_database_at_startup`, `trial tests.storage.test_registration_multitenant.TokenCacheIsolationTestCase`), then implementation, then green.
- **F#2**: TDD is deferred to after reproduce (can't write a meaningful failing test until we know the failing path). Instrumentation commit lands first as a durable guard.
- **Integration**: `docker-demo/` stack, provision 4 tenants, manual `curl` sanity checks.
- **End-to-end**: re-run `docker-demo/stress-test/stress-test-noroom.js` (to verify F#1 + F#3). After F#2 fix, also run `docker-demo/stress-test/stress-test.js` (room-based). Record results in `docker-demo/stress-test/result-2026-04-NN.md`.

## Risks

- **Audit drift.** The F#1 audit might surface cached methods whose fix is architecturally larger than adding `@tenant_cached()` (e.g. cached lists with complex invalidation). Mitigation: if any one case is bigger than 30-40 LoC, document it in the spec and defer it to a follow-up PR rather than bloating this one.
- **F#2 stays unsolved.** Instrumentation lands, reproduce yields inconclusive logs, no fix found. Mitigation: spec explicitly treats instrumentation as a deliverable in its own right; F#3 + F#1 still ship as partial progress.
- **Cache size growth from tenant-aware keys.** Possible memory regression at many-tenant scale. Mitigation: log cache stats before and after in the stress re-run.

## Out-of-scope follow-ups

- Per-tenant cache instances (1c).
- Hot-add / hot-remove of tenants.
- Background-worker tenant-context propagation beyond what F#2 instrumentation surfaces.
- Reviewing every background task for missing tenant context (the audit for F#1 covers *storage caches*, not the full async task graph).
