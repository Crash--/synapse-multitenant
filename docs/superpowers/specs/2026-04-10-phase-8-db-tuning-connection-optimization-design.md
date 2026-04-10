# Phase 8 — Database Tuning & Connection Optimization Design

**Date:** 2026-04-10
**Branch:** `feature/multi-tenant`
**Goal:** Raise the tenant ceiling from ~10-15 to ~100-200 by eliminating
search_path overhead and tuning the shared connection pool.

**Companion docs:**
- `challenges.md` — bottleneck analysis and capacity estimates
- `docs/multi_tenant_roadmap.md` lines 418-449 — phase 8 scope
- `docker-demo/stress-test/` — K6 load test for verification

---

## Problem

Every database operation in multi-tenant mode pays a 3-SQL-command tax:

```
SHOW search_path          -- capture original (round trip 1)
SET search_path TO <schema>  -- switch to tenant (round trip 2)
  ... actual queries ...
SET search_path TO <original>  -- restore (round trip 3)
```

At `cp_max=10`, a pool of 10 connections serves all tenants. Each
connection spends ~67% of its SQL time on schema-switching overhead.
Pool starvation begins at ~10-15 tenants.

**Current code:**
- `_set_tenant_schema()` (database.py:657-702): SHOW + SET, returns original
- `_restore_search_path()` (database.py:758-773): SET back to original
- `runWithConnection()` (database.py:1230-1263): try/finally wrapping every call

---

## Design decisions

### Session-level SET over SET LOCAL

The roadmap originally specified `SET LOCAL search_path`, which scopes to
the current transaction and auto-resets on commit. However, Synapse has
many `db_autocommit=True` call sites (lines 1929, 1978, 2066, 2096, 2174,
2386, 2467, 2508, 2564) where each statement is its own transaction.
`SET LOCAL` in autocommit mode scopes to just the SET statement itself
and resets immediately — the subsequent query would run against the
default search_path, not the tenant's.

**Decision:** Use session-level `SET search_path TO <schema>` instead.
It persists across all statements on the connection regardless of
transaction mode. The connection returns to the pool with the schema
still set. The next borrower will either reuse it (cache hit, sub-phase
8b) or SET to its own tenant (cache miss).

### pgbouncer in session mode

With session-level SET, the search_path is connection state that must
persist across statements within a session. pgbouncer `transaction` mode
would return the connection to the pool between transactions, losing the
search_path. pgbouncer `session` mode pins the connection for the full
client session duration, preserving SET state.

**Decision:** pgbouncer `session` mode. Still provides connection
multiplexing (200 client connections mapped to 50 server connections)
and connection reuse benefits without breaking search_path semantics.

---

## Architecture

### Sub-phase 8a — Eliminate SHOW + restore

**Files changed:** `synapse/storage/database.py` (~15 lines net)

1. **`_set_tenant_schema()`** — Remove the `SHOW search_path` call
   (line 679) and the `original_search_path` return. Method becomes
   `void` — issues a single `SET search_path TO <schema>`.

2. **`_restore_search_path()`** — Delete entirely (lines 758-773).

3. **`runWithConnection()`** (lines 1230-1263) — Remove
   `original_search_path` variable, the finally-block restore call,
   and the conditional guard. The SET happens once at entry; no
   cleanup on exit.

**Result:** 3 SQL commands per `runWithConnection` call → 1.

### Sub-phase 8b — Connection-level schema caching

**Files changed:** `synapse/storage/database.py` (~50 lines net)

1. **Add `_connection_schemas: dict[int, str]`** on `DatabasePool` —
   maps `id(conn)` to the schema name currently SET on that connection.

2. **In `_set_tenant_schema()`** — Before issuing SET, check
   `self._connection_schemas.get(id(conn))`. If it matches the
   requested `tenant.database_schema`, skip the SET entirely and
   return. Otherwise, execute SET and update the cache:
   `self._connection_schemas[id(conn)] = schema`.

3. **Cache invalidation** — In `_on_new_connection()` (line 143,
   the `cp_openfun` callback), remove any stale entry for the
   connection ID. Note: `_on_new_connection` receives the raw
   `psycopg2` connection (same object whose `id()` we cache), so
   the lookup is consistent. This handles connection close/reconnect
   — the next use will always SET.

4. **Optional: metrics** — Add a counter
   `tenant_schema_set_total{cached="true|false"}` to track cache
   hit rate. Useful for stress-test verification.

**Result:** For bursty same-tenant traffic (common case), the SET is
skipped entirely. Only fires on tenant-switch boundaries within a
single connection.

### Sub-phase 8c — Pool tuning + pgbouncer

**Files changed:** Docker configs, documentation

1. **Raise `cp_max`** from 10 to 50 in
   `docker-multitenant/config/homeserver.yaml`. Synapse connects to
   pgbouncer, not Postgres directly, so this is the number of
   connections to pgbouncer.

2. **Add pgbouncer sidecar** to `docker-multitenant/docker-compose.yml`:
   ```yaml
   pgbouncer:
     image: edoburu/pgbouncer:1.21.0
     environment:
       DATABASE_URL: postgres://synapse:synapse_password@postgres:5432/synapse_multitenant
       POOL_MODE: session
       DEFAULT_POOL_SIZE: 50
       MAX_CLIENT_CONN: 200
       MAX_DB_CONNECTIONS: 100
     ports:
       - "16432:6432"
     depends_on:
       postgres:
         condition: service_healthy
     networks:
       - synapse-net
   ```

3. **Update Synapse config** to connect through pgbouncer
   (host: pgbouncer, port: 6432) instead of directly to Postgres.

4. **Raise Postgres `max_connections`** to 200 via
   `POSTGRES_INITDB_ARGS: --max-connections=200` or a custom
   `postgresql.conf`.

5. **Document capacity curve** in `docs/multi_tenant.md` with
   post-phase-8 stress test results.

---

## Verification

### Per-sub-phase probes

**8a probes (unit tests in `tests/tenant/test_database_tuning.py`):**
- `test_no_show_search_path` — Mock the connection cursor, call
  `_set_tenant_schema`, assert `SHOW search_path` is never executed.
- `test_no_restore_search_path` — Assert `_restore_search_path` method
  does not exist on `DatabasePool`.
- `test_tenant_isolation_after_set` — Functional test: SET schema for
  tenant A, query, SET schema for tenant B, query — results are
  isolated.
- `test_autocommit_schema_correct` — Verify that autocommit calls
  (`db_autocommit=True`) use the correct tenant schema.

**8b probes (unit tests in `tests/tenant/test_database_tuning.py`):**
- `test_cache_hit_skips_set` — Call `_set_tenant_schema` twice with
  the same tenant on the same connection. Assert SET is executed once.
- `test_cache_miss_on_tenant_switch` — Call with tenant A then tenant B.
  Assert SET is executed twice.
- `test_cache_invalidation_on_new_connection` — Simulate connection
  reconnect via `_on_new_connection`. Assert cache entry is cleared.

**8c probes (stress test + docker integration):**
- Existing K6 test in `docker-demo/stress-test/` adapted to run at
  5, 10, and 20 tenant load.
- Measure: requests/sec, p95 latency, pgbouncer pool utilization
  (`SHOW POOLS` via pgbouncer admin).
- Pass criteria: 20-tenant load completes without pool exhaustion
  errors; p95 latency < 2x the 5-tenant baseline.

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Connection returns to pool with wrong schema set | Every `runWithConnection` call SETs schema before any query. A stale schema on a pooled connection is always overwritten before use. |
| `id(conn)` reuse after GC (cache collision) | Python `id()` can be reused for different objects. The `_on_new_connection` callback clears stale entries. Additionally, a cache miss just means an unnecessary SET — no correctness risk. |
| pgbouncer session mode limits concurrency | Session mode still multiplexes: 200 client connections share 50 server connections. The bottleneck shifts to Postgres `max_connections`, which we raise to 200. |
| Stress test results vary by hardware | Document results as relative improvements (% change) not absolute numbers. Run before/after on same hardware in same session. |

---

## Out of scope

- Per-tenant connection sub-pools (complexity explosion, defeats shared-pool advantage)
- pgbouncer transaction mode (incompatible with session-level SET)
- Postgres sharding (phase 12+ concern at 500+ tenants)
- Worker-level pool isolation (phase 12)
