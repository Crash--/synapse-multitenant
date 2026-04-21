# Stress-Test Findings Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three 2026-04-20 stress-test findings — tenant registry startup hydration (F#3), cross-tenant token cache leak (F#1), and the createRoom state-group schema leak (F#2) — in dependency order on a dedicated worktree, with the last one preceded by an instrumentation phase whose logs determine the fix shape.

**Architecture:**
- F#3 first: add a tenant-registry method `load_from_database()` and invoke it during `synapse/app/homeserver.py::start()` before the existing tenant-isolation check so the registry is populated before any HTTP traffic is served. Removes the pre-`/reload` fall-through window that pollutes `public`.
- F#1 second: introduce `@tenant_cached()` / `@tenant_cached_list()` in `synapse/util/caches/descriptors.py` that prepend the current tenant's `server_name` to the cache key. Apply to `get_user_by_access_token` and to every other `@cached` on a tenant-scoped read surfaced by a repo-wide audit.
- F#2 last: commit permanent DEBUG-level instrumentation + env-gated (`SYNAPSE_MT_STRICT_STATE_GROUPS=1`) assertions in the state-persistence and tenant-schema-setter paths. Reproduce the `createRoom` failure against the instrumented build, diagnose from the captured logs, then write a targeted fix.

**Tech Stack:**
- Python 3.12, Twisted Trial, `attrs`, `@cached` descriptor family at `synapse/util/caches/descriptors.py`
- PostgreSQL + psycopg2, schema-per-tenant isolation via `search_path`
- Docker Compose demo stack at `docker-demo/`, k6 + Grafana image for stress-test
- Multi-tenant primitives: `synapse/tenant_context.py`, `synapse/tenant_registry.py`, `synapse/config/tenants.py`

**Worktree:** `/home/monta/Documents/workspace/synapse-multitenant/.worktrees/fix-stress-test-findings`
**Branch:** `fix/stress-test-findings` (off `feature/multi-tenant`)

---

## Pre-flight

Every `trial` / `pytest` invocation below assumes the engineer is **in the worktree root** and uses the already-provisioned venv in the main checkout:

```bash
cd /home/monta/Documents/workspace/synapse-multitenant/.worktrees/fix-stress-test-findings
export SYNAPSE_SKIP_RUST_CHECK=1
TRIAL=/home/monta/Documents/workspace/synapse-multitenant/.venv/bin/trial
```

Baseline note: `tests.tenant.test_context` is pre-existing RED (10/13) because `reset_current_tenant()` gained a required `token` argument but the test helpers weren't updated. That failure is OUT OF SCOPE for this plan — **do not** fix it here. Every NEW test we write goes through `tests.tenant.test_registry`-style pattern (plain `unittest.TestCase`) or `HomeserverTestCase` in `tests/unittest.py`.

## File inventory

**Created:**
- `tests/util/caches/test_tenant_cached.py` — unit tests for the new cache decorator
- `tests/storage/test_registration_multitenant.py` — cross-tenant token cache isolation test
- `docs/multi_tenant/cache_audit.md` — output of Task 3 (audit table of every `@cached`/`@cachedList`)
- `docker-demo/stress-test/finding-2-repro-YYYY-MM-DD.md` — Task 8 output, name filled in on the day of the repro
- `docker-demo/stress-test/result-YYYY-MM-DD.md` — Task 10 output

**Modified:**
- `synapse/tenant_registry.py` — add `async def load_from_database(...)` method on `TenantRegistry`
- `synapse/rest/admin/tenants.py:529-552` — delegate to `registry.load_from_database()`
- `synapse/app/homeserver.py` — add hydration call inside `async def start()` before the tenant-isolation check (currently at line ~461)
- `synapse/util/caches/descriptors.py` — add `tenant_cached` and `tenant_cachedList` factories + thin descriptor wrappers
- `synapse/storage/databases/main/registration.py:467` — `@cached()` → `@tenant_cached()` on `get_user_by_access_token`
- Any additional storage methods the Task 3 audit flags as `swap`
- `synapse/storage/database.py` — DEBUG log in `_set_tenant_schema`
- `synapse/storage/databases/state/store.py` — DEBUG logs at sequence + INSERT sites; assertions gated by `SYNAPSE_MT_STRICT_STATE_GROUPS`
- `tests/tenant/test_registry.py` — new test for `load_from_database()`

---

## Task 1 — F#3a: Add `TenantRegistry.load_from_database()` method

**Files:**
- Modify: `synapse/tenant_registry.py` (add new async method on `TenantRegistry`; keep the existing module-level `load_tenants_from_database` as it is already used elsewhere)
- Test: `tests/tenant/test_registry.py` (add one new test)

**Rationale:** The module-level `load_tenants_from_database()` at `synapse/tenant_registry.py:371` already does the DB query + decryption + `MultiTenantConfig` construction. The REST handler wraps it with `db_pool.runWithConnection(...)` + `registry.reload(new_config)` at `synapse/rest/admin/tenants.py:536-558`. Lift that wrapper into `TenantRegistry` so both the REST handler and the new startup hook can call a single method.

- [ ] **Step 1: Write the failing test**

Open `tests/tenant/test_registry.py` and append:

```python
from unittest.mock import AsyncMock, MagicMock


class TestLoadFromDatabase(TestCase):
    """Tests TenantRegistry.load_from_database()."""

    def test_load_from_database_populates_registry(self):
        """Registry populates itself via the method, without an external call to reload()."""
        from twisted.internet.defer import ensureDeferred

        # Start with an empty DB-source registry (the post-startup state today)
        config = MultiTenantConfig(
            enabled=True, default_schema="public", source="database", tenants={}
        )
        registry = TenantRegistry(config)
        self.assertEqual(len(registry.get_all_tenants()), 0)

        # Fake db_pool whose runWithConnection calls the callback with a fake conn
        # whose cursor returns two tenant rows.
        fake_row_acme = {
            "server_name": "acme.com",
            "database_schema": "tenant_acme_com",
            "signing_key_data": b"ed25519 0 abc",
            "media_store_path": "/media/acme",
            "status": "active",
            "signing_key_encrypted": None,
        }
        fake_row_corp = {
            "server_name": "corp.io",
            "database_schema": "tenant_corp_io",
            "signing_key_data": b"ed25519 0 def",
            "media_store_path": "/media/corp",
            "status": "active",
            "signing_key_encrypted": None,
        }
        rows = [tuple(fake_row_acme.values()), tuple(fake_row_corp.values())]
        columns = list(fake_row_acme.keys())

        fake_cursor = MagicMock()
        fake_cursor.description = [(c,) for c in columns]
        fake_cursor.fetchall.return_value = rows

        fake_conn = MagicMock()
        fake_conn.conn.cursor.return_value = fake_cursor

        fake_db_pool = MagicMock()
        fake_db_pool.runWithConnection = AsyncMock(
            side_effect=lambda fn: fn(fake_conn)
        )

        # Run the new method through the Twisted reactor
        result = self.successResultOf(
            ensureDeferred(
                registry.load_from_database(
                    fake_db_pool, master_key=None, default_schema="public"
                )
            )
        )

        # Both tenants now present
        self.assertEqual(
            sorted(t.server_name for t in registry.get_all_tenants()),
            ["acme.com", "corp.io"],
        )
        # reload() return value propagates back
        self.assertIn("added", result)
        self.assertEqual(sorted(result["added"]), ["acme.com", "corp.io"])
```

(Note: `TenantConfig.from_db_row` is already used by the module-level `load_tenants_from_database`; the fake-row keys above must exactly match what that parser expects. If `from_db_row` raises on these rows, adjust the test fixtures to match `synapse/config/tenants.py::TenantConfig.from_db_row` — do NOT change the parser.)

- [ ] **Step 2: Run test — expected FAIL**

```bash
$TRIAL tests.tenant.test_registry.TestLoadFromDatabase
```

Expected: `AttributeError: 'TenantRegistry' object has no attribute 'load_from_database'`.

- [ ] **Step 3: Implement `TenantRegistry.load_from_database`**

Open `synapse/tenant_registry.py`. Inside the `TenantRegistry` class (after `reload()`, before the module-level `load_tenants_from_database` function), add:

```python
    async def load_from_database(
        self,
        db_pool,
        master_key: bytes | None,
        default_schema: str,
    ) -> dict[str, list[str]]:
        """Load tenants from ``public.tenants`` and apply them in-place.

        Thin wrapper around the module-level ``load_tenants_from_database``
        that also runs the DB I/O through ``db_pool`` and calls ``reload()``.

        Args:
            db_pool: A ``DatabasePool`` exposing ``runWithConnection``.
            master_key: AES-256-GCM master key for decrypting signing keys.
            default_schema: Schema name used by ``MultiTenantConfig``.

        Returns:
            The diff dict returned by ``reload()``.
        """

        def _load(conn):
            return load_tenants_from_database(
                conn.conn, master_key, default_schema
            )

        new_config = await db_pool.runWithConnection(_load)
        return self.reload(new_config)
```

- [ ] **Step 4: Run test — expected PASS**

```bash
$TRIAL tests.tenant.test_registry.TestLoadFromDatabase
```

Expected: `PASSED`. Then run the full module to catch regressions:

```bash
$TRIAL tests.tenant.test_registry
```

Expected: all tests pass (13 now — one new).

- [ ] **Step 5: Refactor `ReloadTenantsRestServlet.on_POST` to use the new method**

Open `synapse/rest/admin/tenants.py`. Replace the `if mt_config.source == "database": ... new_config = ...` block (roughly lines 536-552) with a single call:

```python
            if mt_config.source == "database":
                from synapse.crypto.tenant_key_encryption import (
                    get_master_key_from_env,
                )

                master_key = get_master_key_from_env()
                db_pool = self._hs.get_datastores().main.db_pool
                result = await registry.load_from_database(
                    db_pool, master_key, mt_config.default_schema
                )
            else:
                # YAML source: re-read the config file
                self._hs.config.reload_config_section("tenants")
                new_config = self._hs.config.tenants.multi_tenant
                result = registry.reload(new_config)
```

(`load_from_database` calls `self.reload(...)` internally and returns the diff, so the old `result = registry.reload(new_config)` is no longer needed in the DB branch.)

- [ ] **Step 6: Run the full registry + admin test modules**

```bash
$TRIAL tests.tenant.test_registry tests.rest.admin
```

Expected: no regressions.

- [ ] **Step 7: Commit**

```bash
git add synapse/tenant_registry.py synapse/rest/admin/tenants.py tests/tenant/test_registry.py
git commit -m "$(cat <<'EOF'
refactor(tenant_registry): add TenantRegistry.load_from_database()

Lift the DB-load-and-reload wrapper from ReloadTenantsRestServlet into
TenantRegistry so both the REST handler and a future startup hook share
one entry point. Covered by a new unit test.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2 — F#3b: Hydrate the registry at startup

**Files:**
- Modify: `synapse/app/homeserver.py` inside `async def start()` (currently around line 461 where the tenant-isolation check runs)
- Test: `tests/tenant/test_registry.py` — add one more test

**Rationale:** The existing tenant-isolation check at `homeserver.py:461` reads from the registry (`hs.get_tenant_registry().get_all_tenants()` at line 473). In `source=database` deployments the registry is empty at that point, so the isolation check silently loops over nothing. Hydration must land BEFORE the isolation check.

- [ ] **Step 1: Write the failing integration-style test**

This one tests the `start()` hook by constructing a test HS and asserting the registry is populated after the hook runs. Since `start()` is large and does many things, we test the helper it will call instead. Add to `tests/tenant/test_registry.py`:

```python
class TestStartupHydration(TestCase):
    """The helper that hydrates the registry at startup."""

    def test_hydration_helper_invokes_load_from_database_when_db_source(self):
        """When source='database', the helper calls registry.load_from_database()."""
        from synapse.tenant_registry import hydrate_registry_at_startup
        from twisted.internet.defer import ensureDeferred

        config = MultiTenantConfig(
            enabled=True, default_schema="public", source="database", tenants={}
        )
        registry = TenantRegistry(config)

        registry.load_from_database = AsyncMock(return_value={"added": [], "removed": [], "unchanged": []})
        fake_db_pool = MagicMock()

        self.successResultOf(
            ensureDeferred(
                hydrate_registry_at_startup(
                    registry, fake_db_pool, master_key=None
                )
            )
        )
        registry.load_from_database.assert_awaited_once_with(
            fake_db_pool, None, "public"
        )

    def test_hydration_helper_noop_when_yaml_source(self):
        """When source='yaml', the helper does NOT call load_from_database."""
        from synapse.tenant_registry import hydrate_registry_at_startup
        from twisted.internet.defer import ensureDeferred

        config = MultiTenantConfig(
            enabled=True, default_schema="public", source="yaml", tenants={}
        )
        registry = TenantRegistry(config)
        registry.load_from_database = AsyncMock()
        fake_db_pool = MagicMock()

        self.successResultOf(
            ensureDeferred(
                hydrate_registry_at_startup(
                    registry, fake_db_pool, master_key=None
                )
            )
        )
        registry.load_from_database.assert_not_awaited()

    def test_hydration_helper_noop_when_disabled(self):
        """When multi-tenant disabled, the helper does NOT call load_from_database."""
        from synapse.tenant_registry import hydrate_registry_at_startup
        from twisted.internet.defer import ensureDeferred

        config = MultiTenantConfig(enabled=False)
        registry = TenantRegistry(config)
        registry.load_from_database = AsyncMock()
        fake_db_pool = MagicMock()

        self.successResultOf(
            ensureDeferred(
                hydrate_registry_at_startup(
                    registry, fake_db_pool, master_key=None
                )
            )
        )
        registry.load_from_database.assert_not_awaited()
```

- [ ] **Step 2: Run test — expected FAIL**

```bash
$TRIAL tests.tenant.test_registry.TestStartupHydration
```

Expected: `ImportError: cannot import name 'hydrate_registry_at_startup'` on each test.

- [ ] **Step 3: Implement `hydrate_registry_at_startup` helper**

At the bottom of `synapse/tenant_registry.py`, after the existing `create_tenant_registry` function:

```python
async def hydrate_registry_at_startup(
    registry: TenantRegistry,
    db_pool,
    master_key: bytes | None,
) -> None:
    """Populate the registry from ``public.tenants`` once at boot.

    Idempotent no-op when multi-tenant is disabled or when tenants were
    already parsed from YAML (source != "database"). Failures are logged
    at ERROR and swallowed — the process continues to boot with an empty
    registry (same as before this hook existed), and an admin can recover
    via ``POST /_synapse/admin/v1/tenants/reload``.
    """
    if not registry.enabled:
        return
    if registry._config.source != "database":
        return

    try:
        result = await registry.load_from_database(
            db_pool, master_key, registry._config.default_schema
        )
        logger.info(
            "Startup hydration from database: added=%s removed=%s unchanged=%s",
            result.get("added"),
            result.get("removed"),
            result.get("unchanged"),
        )
    except Exception:
        logger.exception(
            "Startup hydration from database failed; registry remains empty. "
            "Admins can recover via POST /_synapse/admin/v1/tenants/reload"
        )
```

- [ ] **Step 4: Run test — expected PASS**

```bash
$TRIAL tests.tenant.test_registry.TestStartupHydration
```

- [ ] **Step 5: Wire the helper into `async def start()`**

Open `synapse/app/homeserver.py`. Find `async def start(` (around line 428). Immediately above the `if hs.config.tenants.multi_tenant.enabled:` block that runs the isolation check (around line 461), add the hydration call:

```python
    # Hydrate the tenant registry from public.tenants for source="database"
    # deployments, so the isolation check below and all HTTP traffic see a
    # populated registry. No-op for YAML-source or single-tenant.
    if hs.config.tenants.multi_tenant.enabled:
        from synapse.crypto.tenant_key_encryption import (
            get_master_key_from_env,
        )
        from synapse.tenant_registry import hydrate_registry_at_startup

        master_key = get_master_key_from_env()
        main_db_pool = hs.get_datastores().main.db_pool
        await hydrate_registry_at_startup(
            hs.get_tenant_registry(), main_db_pool, master_key
        )
```

- [ ] **Step 6: Smoke-test against the docker-demo stack**

```bash
cd docker-demo
docker compose down -v
docker compose up -d
# Wait ~15s for Synapse to finish startup
docker compose logs synapse 2>&1 | grep -E "Tenant registry|Startup hydration"
```

Expected: one "Startup hydration from database" line with `added=['stress-a.localhost', ...]` (or whatever tenants exist in `public.tenants`), and the "Tenant registry initialized" line still shows `0 tenants` (that's the constructor log — hydration runs after). If the demo isn't provisioning tenants during `up`, provision them via the control plane once and re-run `docker compose up -d` (not `up -d --force-recreate`) to restart Synapse with the rows present.

- [ ] **Step 7: Commit**

```bash
git add synapse/tenant_registry.py synapse/app/homeserver.py tests/tenant/test_registry.py
git commit -m "$(cat <<'EOF'
fix(tenant): hydrate registry from public.tenants at startup (F#3)

Honor the documented source='database' contract. Run the DB load once
during synapse.app.homeserver.start(), before the tenant-isolation check
and before the listener is bound. Closes the fall-through window where
Host-matched requests were served as server_name=localhost until an
out-of-band /reload ran.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3 — F#1a: Cache audit inventory

**Files:**
- Create: `docs/multi_tenant/cache_audit.md`

**Rationale:** Before writing the new decorator we enumerate the blast radius. Every `@cached` / `@cachedList` in `synapse/storage/` gets classified so we know the exact list of swaps Task 5 has to perform. This is a non-code task whose output drives subsequent tasks.

- [ ] **Step 1: Enumerate**

```bash
cd /home/monta/Documents/workspace/synapse-multitenant/.worktrees/fix-stress-test-findings
# Output full file:line of every decorator in synapse/storage/
grep -rn --include='*.py' -E '@cached\(|@cachedList\(' synapse/storage/ > /tmp/cache_decorators.txt
wc -l /tmp/cache_decorators.txt
```

- [ ] **Step 2: Classify each entry**

For each line in `/tmp/cache_decorators.txt`, open the file, read the decorated method, and classify into one of:

| Class | Criterion |
|---|---|
| `swap` | Reads a tenant-scoped table AND cache key does NOT naturally include tenant-disambiguating data |
| `safe-explicit` | Cache key includes data that disambiguates per tenant (e.g. fully-qualified `user_id` like `@user:acme.com`, or a per-tenant room_id) |
| `not-tenant-scoped` | Reads a table that is in `public` only (shared state, never cloned into tenant schemas) |

Tenant-scoped tables: anything that gets cloned by `scripts/create_tenant_schema.py`. Spot-check: `access_tokens`, `users`, `devices`, `state_groups`, `events`, `rooms`, `event_json`, `membership` are tenant-scoped. `state_groups_persisting` was suspect during F#2 investigation — mark it as tenant-scoped.

Tables in `public` only (do NOT clone): `tenants`, `tenant_keys`, and anything under `scripts/create_tenant_schema.py`'s explicit exclusion list.

Document edge cases inline. If a method takes a `user_id` that COULD be either a local-part or a fully-qualified ID depending on caller, mark `swap` (conservative).

- [ ] **Step 3: Write the audit markdown**

Create `docs/multi_tenant/cache_audit.md` with this exact layout:

```markdown
# Storage Cache Multi-Tenancy Audit

**Date:** 2026-04-21
**Scope:** Every `@cached` / `@cachedList` decorator in `synapse/storage/`.
**Goal:** Identify which need to swap to `@tenant_cached` / `@tenant_cached_list` to close Finding #1.

## Classification

| File:Line | Method | Table | Key args | Class | Reasoning |
|---|---|---|---|---|---|
| `synapse/storage/databases/main/registration.py:467` | `get_user_by_access_token` | `access_tokens` | `token` | **swap** | Bare token lookup, no tenant context in key → leaks across tenants |
| ...one row per enumerated entry... |

## Summary

- Total decorators: N
- `swap`: M
- `safe-explicit`: K
- `not-tenant-scoped`: L

## Actionable list (Task 5 input)

The `swap` entries below get handled one-by-one in Task 5:

1. `synapse/storage/databases/main/registration.py:467` — `get_user_by_access_token`
2. ...
```

- [ ] **Step 4: Commit**

```bash
git add docs/multi_tenant/cache_audit.md
git commit -m "$(cat <<'EOF'
docs(multi_tenant): cache audit for cross-tenant leak fix (F#1)

Enumerate every @cached / @cachedList decorator in synapse/storage/ and
classify each as swap / safe-explicit / not-tenant-scoped. Drives the
list of decorator swaps in the next tasks.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4 — F#1b: Create `@tenant_cached` / `@tenant_cached_list` decorators

**Files:**
- Modify: `synapse/util/caches/descriptors.py`
- Create: `tests/util/caches/test_tenant_cached.py`

**Rationale:** Closest-to-zero-risk fix surface: wrap the existing `_CachedFunctionDescriptor` with a new outer factory that prepends the current tenant's `server_name` (or `None` in non-MT mode) to every cache key. All other semantics (LRU, invalidation, eviction, `cachedList` batching) inherit from the existing descriptor.

- [ ] **Step 1: Write the failing cross-tenant isolation test**

Create `tests/util/caches/test_tenant_cached.py`:

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#
from unittest import TestCase

from synapse.tenant_context import (
    get_current_tenant,
    set_current_tenant,
    reset_current_tenant,
)
from synapse.config.tenants import TenantConfig
from synapse.util.caches.descriptors import tenant_cached


class _Store:
    """Minimal object with a tenant_cached method for decorator testing."""

    def __init__(self):
        self.call_count = 0

    @tenant_cached()
    async def lookup(self, token: str) -> str:
        self.call_count += 1
        tenant = get_current_tenant()
        suffix = tenant.server_name if tenant else "default"
        return f"{token}@{suffix}"


class TenantCachedTestCase(TestCase):
    def _tenant(self, name: str) -> TenantConfig:
        return TenantConfig(
            server_name=name,
            database_schema=f"tenant_{name.replace('.', '_')}",
            signing_key_path=f"/keys/{name}.key",
            media_store_path=f"/media/{name}",
        )

    def test_same_token_different_tenants_is_not_a_cache_hit(self):
        from twisted.internet.defer import ensureDeferred

        store = _Store()
        a = self._tenant("a.test")
        b = self._tenant("b.test")

        # Lookup under tenant A
        tok_a = set_current_tenant(a)
        try:
            ra = self.successResultOf(ensureDeferred(store.lookup("TOKEN")))
        finally:
            reset_current_tenant(tok_a)

        # Lookup under tenant B with the same token string
        tok_b = set_current_tenant(b)
        try:
            rb = self.successResultOf(ensureDeferred(store.lookup("TOKEN")))
        finally:
            reset_current_tenant(tok_b)

        # Both calls executed (cache did NOT cross tenants)
        self.assertEqual(store.call_count, 2)
        self.assertEqual(ra, "TOKEN@a.test")
        self.assertEqual(rb, "TOKEN@b.test")

    def test_repeat_within_same_tenant_hits_cache(self):
        from twisted.internet.defer import ensureDeferred

        store = _Store()
        a = self._tenant("a.test")

        tok = set_current_tenant(a)
        try:
            r1 = self.successResultOf(ensureDeferred(store.lookup("TOKEN")))
            r2 = self.successResultOf(ensureDeferred(store.lookup("TOKEN")))
        finally:
            reset_current_tenant(tok)

        self.assertEqual(store.call_count, 1)  # second call was a cache hit
        self.assertEqual(r1, r2)

    def test_no_tenant_context_uses_none_key(self):
        from twisted.internet.defer import ensureDeferred

        store = _Store()
        r1 = self.successResultOf(ensureDeferred(store.lookup("TOKEN")))
        r2 = self.successResultOf(ensureDeferred(store.lookup("TOKEN")))
        self.assertEqual(store.call_count, 1)
        self.assertEqual(r1, "TOKEN@default")
        self.assertEqual(r1, r2)
```

- [ ] **Step 2: Run test — expected FAIL**

```bash
$TRIAL tests.util.caches.test_tenant_cached
```

Expected: `ImportError: cannot import name 'tenant_cached' from 'synapse.util.caches.descriptors'`.

- [ ] **Step 3: Implement `tenant_cached` + `tenant_cached_list`**

Open `synapse/util/caches/descriptors.py`. At the bottom of the file (after `cachedList` — line ~595 in the current file), add:

```python
def tenant_cached(
    *,
    max_entries: int = 1000,
    num_args: int | None = None,
    uncached_args: Collection[str] | None = None,
    tree: bool = False,
    cache_context: bool = False,
    iterable: bool = False,
    prune_unread_entries: bool = True,
    name: str | None = None,
) -> _CachedFunctionDescriptor:
    """Tenant-aware variant of ``@cached``.

    Behaves identically to ``@cached`` in all respects (LRU sizing,
    invalidation, eviction, tree keys, iterable values) except that the
    cache key is prepended with the current tenant's ``server_name``
    (or ``None`` outside any tenant context). This prevents a value
    computed under tenant A from being returned to a lookup under tenant B
    when both use the same remaining arguments (e.g. the same access token).

    See ``docs/multi_tenant/cache_audit.md`` for which methods need this
    variant and why.
    """
    from synapse.tenant_context import get_current_tenant

    inner = _CachedFunctionDescriptor(
        max_entries=max_entries,
        num_args=num_args,
        uncached_args=uncached_args,
        tree=tree,
        cache_context=cache_context,
        iterable=iterable,
        prune_unread_entries=prune_unread_entries,
        name=name,
    )

    def _wrap(orig):
        cached_fn = inner(orig)

        async def tenant_aware(self, *args, **kwargs):
            tenant = get_current_tenant()
            tenant_key = tenant.server_name if tenant is not None else None
            # Bind the tenant key into the cache lookup by prepending it as a
            # positional argument. The underlying descriptor builds its key
            # from positional args, so this naturally partitions.
            return await cached_fn(self, tenant_key, *args, **kwargs)

        # Re-expose the underlying cache for invalidation APIs.
        tenant_aware.cache = cached_fn.cache  # type: ignore[attr-defined]
        tenant_aware.invalidate = cached_fn.invalidate  # type: ignore[attr-defined]
        tenant_aware.invalidate_all = cached_fn.invalidate_all  # type: ignore[attr-defined]
        return tenant_aware

    return _wrap  # type: ignore[return-value]


def tenant_cached_list(
    *,
    cached_method_name: str,
    list_name: str,
    num_args: int | None = None,
    name: str | None = None,
) -> _CachedListFunctionDescriptor:
    """Tenant-aware variant of ``@cachedList``.

    Applies the same key-prefixing treatment. The paired ``cached_method_name``
    must itself be decorated with ``@tenant_cached`` so that the per-item
    lookups share the same tenant-prefixed key.
    """
    # Implementation deferred — no audit-surfaced `@cachedList` currently
    # needs this wrapper. If Task 5 turns one up, implement here mirroring
    # tenant_cached's wrap-and-re-expose pattern.
    raise NotImplementedError(
        "tenant_cached_list is a placeholder; populate when the audit surfaces a user."
    )
```

**NOTE ON SIGNATURE:** the wrapper above assumes the decorated method's positional args start with `self`. If any `swap` target takes keyword-only args, adapt the wrapper at that site — don't change the decorator.

**NOTE ON KEY LAYOUT:** the cache key becomes `(tenant_server_name, *original_args)`. When `num_args` is explicitly set on the decorator, the prepended tenant key counts as an arg too — decorators using `num_args` will need `num_args += 1` at the call site. Task 5 accounts for this per method.

- [ ] **Step 4: Run test — expected PASS**

```bash
$TRIAL tests.util.caches.test_tenant_cached
```

All three tests pass.

- [ ] **Step 5: Run the original descriptors test module for regressions**

```bash
$TRIAL tests.util.caches
```

Expected: all existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add synapse/util/caches/descriptors.py tests/util/caches/test_tenant_cached.py
git commit -m "$(cat <<'EOF'
feat(caches): add @tenant_cached decorator (F#1 foundation)

Wrap the existing @cached descriptor with a tenant-aware outer layer
that prepends get_current_tenant().server_name to the cache key.
Single-tenant behavior unchanged (tenant key = None). Cross-tenant
isolation proven by unit test.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5 — F#1c: Swap `get_user_by_access_token` + audit follow-ups

**Files:**
- Modify: `synapse/storage/databases/main/registration.py:467`
- Create: `tests/storage/test_registration_multitenant.py`
- Modify: any additional `swap`-classified methods from Task 3's audit

**Rationale:** Ship the primary security fix under a focused test, then apply the same mechanical swap to every `swap` entry from the audit.

- [ ] **Step 1: Write the failing cross-tenant token cache isolation test**

Create `tests/storage/test_registration_multitenant.py`:

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#
from unittest.mock import AsyncMock

from synapse.config.tenants import TenantConfig
from synapse.tenant_context import set_current_tenant, reset_current_tenant
from synapse.storage.databases.main.registration import TokenLookupResult
from tests.unittest import HomeserverTestCase


class TokenCacheCrossTenantIsolationTestCase(HomeserverTestCase):
    """F#1: a token minted for tenant A must not satisfy a lookup in tenant B."""

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastores().main

    def _tenant(self, name: str) -> TenantConfig:
        return TenantConfig(
            server_name=name,
            database_schema=f"tenant_{name.replace('.', '_')}",
            signing_key_path=f"/keys/{name}.key",
            media_store_path=f"/media/{name}",
        )

    def test_same_token_different_tenants_returns_different_results(self):
        # Stub the underlying DB call to reflect which tenant it was invoked in.
        invocations = []

        async def fake_run_interaction(desc, fn, *args, **kwargs):
            from synapse.tenant_context import get_current_tenant
            tenant = get_current_tenant()
            invocations.append(tenant.server_name if tenant else None)
            if tenant and tenant.server_name == "a.test":
                return TokenLookupResult(user_id="@u0:a.test", device_id="D", valid_until_ms=None, token_id=1, token_owner="@u0:a.test", token_used=False)
            if tenant and tenant.server_name == "b.test":
                return None
            return None

        self.store.db_pool.runInteraction = fake_run_interaction

        a = self._tenant("a.test")
        b = self._tenant("b.test")

        # Populate cache under tenant A
        tok_a = set_current_tenant(a)
        try:
            ra = self.get_success(self.store.get_user_by_access_token("TOK"))
        finally:
            reset_current_tenant(tok_a)

        self.assertIsNotNone(ra)
        self.assertEqual(ra.user_id, "@u0:a.test")
        self.assertEqual(invocations, ["a.test"])

        # Lookup the SAME token under tenant B — MUST re-run the DB call, MUST NOT return A's row
        tok_b = set_current_tenant(b)
        try:
            rb = self.get_success(self.store.get_user_by_access_token("TOK"))
        finally:
            reset_current_tenant(tok_b)

        self.assertIsNone(rb)
        self.assertEqual(invocations, ["a.test", "b.test"])  # both tenants queried the DB
```

- [ ] **Step 2: Run test — expected FAIL**

```bash
$TRIAL tests.storage.test_registration_multitenant
```

Expected: the second lookup under tenant B returns the cached A-row, so `rb.user_id == "@u0:a.test"` and `invocations == ["a.test"]`. Assertion failure.

- [ ] **Step 3: Swap `@cached` → `@tenant_cached` at registration.py:467**

Open `synapse/storage/databases/main/registration.py`. At line 467 change:

```python
    @cached()
    async def get_user_by_access_token(self, token: str) -> TokenLookupResult | None:
```

to:

```python
    @tenant_cached()
    async def get_user_by_access_token(self, token: str) -> TokenLookupResult | None:
```

And add `tenant_cached` to the import at the top of the file (find the existing `from synapse.util.caches.descriptors import cached` or `cached, cachedList` line and append `tenant_cached`):

```python
from synapse.util.caches.descriptors import cached, cachedList, tenant_cached
```

- [ ] **Step 4: Run test — expected PASS**

```bash
$TRIAL tests.storage.test_registration_multitenant
```

- [ ] **Step 5: Run the broader registration + auth test modules for regressions**

```bash
$TRIAL tests.storage.databases.main.test_registration tests.api.test_auth tests.rest.client.test_login
```

Expected: no regressions.

- [ ] **Step 6: Commit the primary fix**

```bash
git add synapse/storage/databases/main/registration.py tests/storage/test_registration_multitenant.py
git commit -m "$(cat <<'EOF'
fix(auth): tenant-scope the access-token lookup cache (F#1)

@cached() on get_user_by_access_token used (token,) as its key, so a
token minted for tenant A was returned as a cache hit for tenant B's
Host header. Swap to @tenant_cached() which prepends the active
tenant's server_name to the key. Closes the tenant isolation boundary
violation reported in docker-demo/stress-test/results.md (2026-04-20).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 7: Apply remaining swaps from the audit**

For EACH entry in the "Actionable list" section of `docs/multi_tenant/cache_audit.md` **other than** `get_user_by_access_token`:

1. Read the method body.
2. Write a focused failing test in a suitable `tests/storage/test_<module>_multitenant.py` following the pattern in step 1. If the method is too deep to unit-test, a mocked test at the store level is sufficient; the point is proving cross-tenant isolation, not exercising real SQL.
3. Swap `@cached` → `@tenant_cached` (and add the import if needed).
4. Run the new test + the file's existing test module.
5. Commit with message `fix(<subsystem>): tenant-scope <method> cache (F#1 audit #N)` — increment N per commit.

Stop if you find a method whose current cache key already disambiguates per tenant (should have been `safe-explicit` in the audit but slipped through) — do NOT swap; go update the audit file instead.

**Budget guard:** if the audit's `swap` count exceeds ~10, or if any single swap needs a non-trivial refactor (e.g. callers need updating, the method has a complex `num_args` tree key), STOP and escalate: document the remaining items in the audit under "deferred to follow-up PR" and keep the scope of this PR finite.

---

## Task 6 — F#2a: State-persistence instrumentation

**Files:**
- Modify: `synapse/storage/database.py` — DEBUG log in `_set_tenant_schema`
- Modify: `synapse/storage/databases/state/store.py` — DEBUG logs at sequence/INSERT sites, env-gated assertions

**Rationale:** The instrumentation is a permanent guard, not scaffolding. Commit everything the repro run will need; also commit the `SYNAPSE_MT_STRICT_STATE_GROUPS=1` assertions so CI can enforce no regressions once F#2 is fixed.

- [ ] **Step 1: Locate `_set_tenant_schema`**

```bash
grep -n '_set_tenant_schema' synapse/storage/database.py
```

Note the line number. Read the function — it sets `search_path` per transaction based on `get_current_tenant()`.

- [ ] **Step 2: Add a DEBUG log line inside `_set_tenant_schema`**

Insert at the END of the function, right before `return`:

```python
        logger.debug(
            "txn=%s tenant=%s search_path=%s",
            getattr(cursor, "connection", "?"),
            tenant.server_name if tenant else None,
            schema,
        )
```

(Adapt the first two arg values to whatever locals are in scope — read the function and pick faithful identifiers.)

- [ ] **Step 3: Locate the state-group persistence INSERTs**

```bash
grep -n "INSERT INTO state_groups\|'state_groups'\|\"state_groups\"" synapse/storage/databases/state/store.py
grep -n "nextval\|_state_group_seq_gen\|get_next_id_txn" synapse/storage/databases/state/store.py
```

Read the relevant functions (`build_sequence_generator` call site, `insert_delta_group_txn`, `_store_state_group_txn`, any writes to `state_groups_persisting` and `state_group_edges`).

- [ ] **Step 4: Add a schema-resolution helper**

At the top of `synapse/storage/databases/state/store.py`, after the imports, add:

```python
def _resolve_target_schema(txn, table: str) -> str | None:
    """Return the PG-resolved schema for ``table`` under the current search_path.

    Used for instrumentation: gives us the schema PostgreSQL WILL write into
    for an unqualified INSERT, which is what we need to diagnose F#2-style
    leaks where sequence and INSERT disagree on schema.
    """
    try:
        txn.execute(
            "SELECT n.nspname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = %s "
            "AND n.nspname = ANY(current_schemas(false)) "
            "ORDER BY array_position(current_schemas(false), n.nspname) "
            "LIMIT 1",
            (table,),
        )
        row = txn.fetchone()
        return row[0] if row else None
    except Exception:
        return None
```

- [ ] **Step 5: Instrument the sequence-init site**

Inside `__init__` of `StateGroupDataStore` (or wherever `build_sequence_generator` for `state_group_id_seq` is called), add ONE INFO log right after the generator is built:

```python
        logger.info(
            "state_group_id_seq generator initialized at startup "
            "(no tenant context active); check_consistency executes under "
            "search_path=%s",
            "public",  # startup's effective search_path
        )
```

- [ ] **Step 6: Instrument the runtime INSERT / nextval sites**

For each `nextval` call and each `db_pool.simple_insert_txn(txn, table="state_groups"|"state_group_edges"|"state_groups_persisting", ...)` in `state/store.py`, add immediately before the call:

```python
        from synapse.tenant_context import get_current_tenant
        _tenant = get_current_tenant()
        _sp = None
        _sg_target = None
        try:
            txn.execute("SELECT current_setting('search_path')")
            _sp = txn.fetchone()[0]
        except Exception:
            pass
        _sg_target = _resolve_target_schema(txn, "state_groups")
        logger.debug(
            "state_groups op: tenant=%s search_path=%s pg_resolves_state_groups_to=%s op=%s",
            _tenant.server_name if _tenant else None,
            _sp,
            _sg_target,
            "<describe-this-site>",  # e.g. "nextval" / "insert state_groups" / "insert state_groups_persisting"
        )

        if os.environ.get("SYNAPSE_MT_STRICT_STATE_GROUPS"):
            assert _tenant is not None, (
                f"SYNAPSE_MT_STRICT_STATE_GROUPS: state_groups write attempted "
                f"with no tenant context. search_path={_sp} target_schema={_sg_target}"
            )
```

Factor this block into a helper `_mt_check_state_groups_write(txn, op: str)` at the top of the file to keep the call sites compact. Add `import os` at the top if not already imported.

- [ ] **Step 7: Run unit tests for regressions**

```bash
$TRIAL tests.storage.databases.state tests.storage.test_state tests.tenant
```

Expected: no regressions (instrumentation is log + optional-assert — observable behavior unchanged by default).

- [ ] **Step 8: Commit**

```bash
git add synapse/storage/database.py synapse/storage/databases/state/store.py
git commit -m "$(cat <<'EOF'
chore(state): instrument state-group persistence for F#2 diagnosis

Add DEBUG logs at sequence and INSERT sites in state/store.py showing
(tenant, search_path, pg-resolved-target-schema, op). Also gate an
assertion on SYNAPSE_MT_STRICT_STATE_GROUPS=1 that fails any write to
state_groups / state_group_edges / state_groups_persisting with no
tenant context — off by default, on for the F#2 repro and CI.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7 — F#2b: Reproduce the bug with instrumentation and diagnose

**Files:**
- Create: `docker-demo/stress-test/finding-2-repro-2026-04-21.md` (rename to actual date of execution)

**Rationale:** Run the real stack with F#3 + F#1 already merged (so no pre-reload pollution window) and `SYNAPSE_MT_STRICT_STATE_GROUPS=1`. Trigger one `createRoom`, collect the logs, decide which of the three spec hypotheses the evidence supports, and write it down. **This task produces a diagnosis document, not a code fix.**

- [ ] **Step 1: Configure the demo stack to export the env var**

Open `docker-demo/docker-compose.yml`. Under the `synapse` service, in the `environment:` block, add:

```yaml
      SYNAPSE_MT_STRICT_STATE_GROUPS: "1"
```

(Do NOT commit this — it stays as a local tweak during repro. We'll document "set this env var during diagnosis" in the followup PR if useful.)

- [ ] **Step 2: Bring up a clean stack**

```bash
cd docker-demo
docker compose down -v
docker compose up -d
# Wait until synapse logs "SynapseSite starting on" (readiness).
docker compose logs -f --tail=0 synapse &
LOG_PID=$!
sleep 20
kill $LOG_PID 2>/dev/null
```

- [ ] **Step 3: Provision one tenant + one user via the control plane (existing path)**

```bash
curl -k -X POST -H "Content-Type: application/json" \
  -H "Host: control.localhost" \
  https://localhost/api/tenants \
  -d '{"server_name":"stress-a.localhost","registration_enabled":true}'
# (adapt to the control-plane schema currently in use; consult the
#  existing docker-demo/stress-test/setup.py or setup_noroom.py for
#  the precise request shape and required fields.)

# Register one user
python docker-demo/stress-test/setup_noroom.py  # creates users against stress-a
```

Verify the registry hydrated on startup (F#3 already shipped):

```bash
docker compose logs synapse 2>&1 | grep "Startup hydration"
```

- [ ] **Step 4: Attempt one `createRoom` and capture logs**

```bash
# Login
TOK=$(curl -sk -H "Host: stress-a.localhost" \
  -H "Content-Type: application/json" \
  -X POST https://localhost/_matrix/client/v3/login \
  -d '{"type":"m.login.password","user":"user0","password":"stresstest"}' \
  | jq -r .access_token)

# Trigger the bug
curl -sk -H "Host: stress-a.localhost" \
  -H "Authorization: Bearer $TOK" \
  -H "Content-Type: application/json" \
  -X POST https://localhost/_matrix/client/v3/createRoom \
  -d '{"name":"f2-repro"}' \
  > /tmp/createRoom.resp

cat /tmp/createRoom.resp
```

Expect 500 (current behavior).

- [ ] **Step 5: Dump and filter the instrumented logs**

```bash
docker compose logs synapse 2>&1 > /tmp/f2-full.log
grep -E "tenant=|search_path|state_groups|state_group_id_seq|pg_resolves|SYNAPSE_MT_STRICT_STATE_GROUPS|UniqueViolation|unpersisted prev_group" \
  /tmp/f2-full.log > /tmp/f2-filtered.log
wc -l /tmp/f2-filtered.log
```

- [ ] **Step 6: Dump the DB state**

```bash
docker compose exec -T postgres psql -U synapse -d synapse -c \
  "SELECT 'public.state_groups' AS src, COUNT(*), COALESCE(MIN(id), 0), COALESCE(MAX(id), 0) FROM public.state_groups
   UNION ALL
   SELECT 'tenant.state_groups' AS src, COUNT(*), COALESCE(MIN(id), 0), COALESCE(MAX(id), 0) FROM tenant_stress_a_localhost.state_groups
   UNION ALL
   SELECT 'public.state_group_id_seq' AS src, -1, -1, last_value FROM public.state_group_id_seq
   UNION ALL
   SELECT 'tenant.state_group_id_seq' AS src, -1, -1, last_value FROM tenant_stress_a_localhost.state_group_id_seq;"
```

- [ ] **Step 7: Write the diagnosis file**

Create `docker-demo/stress-test/finding-2-repro-<YYYY-MM-DD>.md`:

```markdown
# F#2 reproduce — <date>

**Branch:** `fix/stress-test-findings` @ <short-sha-after-tasks-1-6>
**Env:** `docker-demo/` with `SYNAPSE_MT_STRICT_STATE_GROUPS=1`
**Trigger:** single `POST /_matrix/client/v3/createRoom` against `stress-a.localhost`
**Prereqs:** F#3 (startup hydration) and F#1 (tenant-scoped token cache) already in effect.

## Observed

- createRoom response: <status code> — <excerpt>
- Assertion fired? <yes/no, and which one>
- Per-endpoint timings: <if relevant>

## DB state after one failed createRoom

<paste the UNION ALL query results>

## Log timeline (key lines only)

<paste the first 20-30 relevant lines from /tmp/f2-filtered.log, in chronological order>

## Diagnosis

<one of>
- **Hypothesis A (startup writes to public)** supported by: [cite log lines showing state_groups writes under tenant=None]
- **Hypothesis B (search_path drops `public`)** supported by: [cite log lines showing search_path set to tenant-only and pg_resolves target to something unexpected]
- **Hypothesis C (global state_groups_persisting tracker)** supported by: [cite log lines showing tracker conflict across schemas]

Confidence: <low/medium/high>

## Fix shape (input to Task 8)

<brief bullet list: exact code locations and the change we'll make, derived from the evidence above>
```

- [ ] **Step 8: Commit the diagnosis**

```bash
git add docker-demo/stress-test/finding-2-repro-*.md
git commit -m "$(cat <<'EOF'
docs(stress): F#2 reproduce + diagnosis

Ran the instrumented build against a clean docker-demo stack with
SYNAPSE_MT_STRICT_STATE_GROUPS=1 after F#3 and F#1 landed. Logs +
DB state in the attached markdown; pins which of the three hypotheses
is responsible and lists the code sites for the Task 8 fix.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Checkpoint:** do NOT proceed to Task 8 until a human (or the controlling agent) has read the diagnosis file and signed off on the fix shape. Task 8's steps below are *templates* — concrete code goes in during the sign-off step.

---

## Task 8 — F#2c: Fix the state-group leak (shape confirmed by Task 7)

**Files:** determined by Task 7's "Fix shape" section. Common shapes:

- **If Hypothesis A:** the fix likely lives in whatever startup path is writing to `state_groups` with no tenant context. Options:
  - Defer that write until the first tenant request under a proper context.
  - Refactor to run the seeding inside `with tenant_context(...)` for every configured tenant at boot.
  - Most likely code site: `synapse/storage/databases/state/store.py::__init__` and/or any migrations that INSERT rows.
- **If Hypothesis B:** `synapse/storage/database.py::_set_tenant_schema` — change `SET search_path TO {schema}` to `SET search_path TO {schema}, public` (per `docs/multi_tenant.md:319-320`). Small diff but review the "security-first" comment immediately above it for historical reasons to keep tenant-only; if a reason still applies, prefer a narrower fix (e.g. `, public` only for specific tables via schema-qualified SQL).
- **If Hypothesis C:** partition `state_groups_persisting` tracking per tenant. Likely an in-memory dict keyed on tenant `server_name`. Code site: wherever the Python-side tracker lives (`_persisting_state_groups` or similar).

The steps below are the generic TDD shape; instantiate with concrete code during sign-off.

- [ ] **Step 1: Write the failing regression test**

Location depends on hypothesis:
- A: `tests/storage/test_state_groups_mt_startup.py` — assert no writes to `public.state_groups` during construction of `StateGroupDataStore` in multi-tenant mode.
- B: `tests/tenant/test_search_path.py` — assert `_set_tenant_schema` emits the expected SQL for the current design.
- C: `tests/storage/test_state_groups_persisting_mt.py` — assert two concurrent persists for different tenants at the same numeric state-group ID do not collide.

Fill in the test from the diagnosis file's "Fix shape" section.

- [ ] **Step 2: Run — expected FAIL**

- [ ] **Step 3: Implement the fix**

Exactly the diff described in Task 7's "Fix shape" section. If the fix is larger than ~50 LoC or touches multiple subsystems, STOP and escalate — likely means the diagnosis was wrong or the chosen shape was under-scoped.

- [ ] **Step 4: Run — expected PASS**

Also run the broader state + tenant modules:

```bash
$TRIAL tests.storage.databases.state tests.storage.test_state tests.tenant
```

- [ ] **Step 5: Repeat the docker-demo repro from Task 7**

Same steps 2-6 from Task 7. Expected: createRoom returns 200, DB shows rows only in the tenant schema, no assertion fired, no UniqueViolations in the logs.

- [ ] **Step 6: Commit**

Commit message format:

```bash
git commit -m "$(cat <<'EOF'
fix(state): <one-line description of the chosen fix> (F#2)

<2-3 lines describing which hypothesis the repro confirmed and what
the fix does. Reference docker-demo/stress-test/finding-2-repro-*.md.>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9 — End-to-end stress-test verification

**Files:**
- Create: `docker-demo/stress-test/result-<YYYY-MM-DD>.md`

**Rationale:** Prove all three fixes hold under load at the same profile as the 2026-04-20 baseline. This is the acceptance test for the whole PR.

- [ ] **Step 1: Clean docker-demo boot + baseline provisioning**

```bash
cd docker-demo
docker compose down -v
docker compose up -d
sleep 20
# Provision 4 tenants stress-{a..d}.localhost + 10 users each, same as the 2026-04-20 baseline
python stress-test/setup_noroom.py
```

Verify: `docker compose logs synapse | grep "Startup hydration"` shows 4 tenants added.

- [ ] **Step 2: Run the no-room variant (tests F#1 + F#3)**

```bash
cd stress-test
./run.sh stress-test-noroom.js   # adjust if run.sh takes other args
# Wait ~5 minutes for the run to complete.
```

Check the k6 summary. Required:

| Metric | Target |
|---|---|
| Cross-tenant token rejected | ≥ 99% |
| login 200 | ≥ 99% |
| whoami 200 | ≥ 99% |
| whoami user matches tenant | ≥ 99% |
| sync 200 | ≥ 99% |
| p95 http_req_duration | < 50 ms |

If cross-tenant-rejection is lower, F#1 regressed — do not proceed; re-run Task 3 and Task 5.

- [ ] **Step 3: Run the room-based variant (tests F#2)**

```bash
./run.sh stress-test.js  # the original, room-based one
```

Required: k6 completes without createRoom errors in the setup, and the message-send checks pass ≥99%.

- [ ] **Step 4: DB sanity check**

```bash
docker compose exec -T postgres psql -U synapse -d synapse -c \
  "SELECT schemaname, relname, n_live_tup
   FROM pg_stat_user_tables
   WHERE relname IN ('state_groups','state_group_edges','state_groups_persisting','access_tokens','users')
   ORDER BY relname, schemaname;"
```

Expected:
- `access_tokens`, `users` rows exist ONLY in `tenant_*` schemas (none added to `public` during the run).
- `state_groups`, `state_group_edges`, `state_groups_persisting` rows exist ONLY in `tenant_*` schemas.

If any tenant-scoped table accumulated rows in `public`, F#2 or F#3 regressed.

- [ ] **Step 5: Write the results file**

Mirror the structure of `docker-demo/stress-test/results.md` (2026-04-20) — same sections, same tables, populated with today's numbers. Call out the delta versus baseline:

```markdown
# Stress-test results — <YYYY-MM-DD>  (post fix/stress-test-findings merge)

...

## Delta vs 2026-04-20 baseline

| Metric | 2026-04-20 | This run | Direction |
|---|---|---|---|
| Cross-tenant token rejected | 27% | XX% | ↑ |
| whoami 200 | 73% | XX% | ↑ |
| ... | | | |

## Findings status

- F#1 (cross-tenant token leak): **closed** — cross-tenant check rate ≥ 99% under 200 VUs.
- F#2 (state-group schema leak): **closed** — createRoom runs to completion; no rows in `public.state_groups` post-run.
- F#3 (registry empty on startup): **closed** — `Startup hydration from database` log visible on `docker compose up`; no `/reload` call made during test harness.
```

- [ ] **Step 6: Commit the results**

```bash
git add docker-demo/stress-test/result-*.md
git commit -m "$(cat <<'EOF'
test(stress): 2026-04-NN post-fix results

All three findings from 2026-04-20 closed. Cross-tenant isolation
check, login, whoami, and sync pass rates ≥99% at 200 VUs; room-based
createRoom workload runs end-to-end; no rows accumulate in public for
tenant-scoped tables.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Exit criteria

All below must be true before the PR is ready:

- [ ] All unit tests added by Tasks 1, 2, 4, 5 pass locally.
- [ ] Docker-demo smoke test from Task 2 Step 6 shows `Startup hydration` log.
- [ ] Diagnosis file from Task 7 committed with hypothesis and evidence.
- [ ] Task 8 fix committed; its regression test passes.
- [ ] Task 9's `docker-demo/stress-test/result-*.md` committed with pass-rate metrics meeting the targets.
- [ ] No new failures in `trial tests.tenant tests.storage tests.api tests.rest.client.test_login tests.util.caches`.
- [ ] Cache audit committed at `docs/multi_tenant/cache_audit.md`.
