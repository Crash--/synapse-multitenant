# F#2 reproduce — 2026-04-21

**Branch:** `fix/stress-test-findings` @ `cb4b6d4ad`
**Env:** `docker-demo/` with the rebuilt `synapse:multi-tenant` image (F#3 + F#1 Groups A/B/C + F#2 instrumentation)
**Trigger:** single `POST /_matrix/client/v3/createRoom` against `stress-a.localhost`
**Prereqs:** F#3 startup hydration and F#1 cache swaps already in effect.

## TL;DR

**The originally-reported F#2 symptom is GONE.** The 2026-04-20 baseline showed 17 leaked `state_groups` rows in `public` plus a `tenant.state_group_id_seq` advanced to 20. With F#3 (startup hydration) in place, there is no pre-`/reload` fall-through window, and the state_groups persistence path writes correctly to the tenant schema.

**A different F#2-class leak remains.** `createRoom` still fails with HTTP 500 — but now on a different code path. The `rooms` table INSERT ends up in `public`, while `state_groups` INSERTs correctly land in `tenant`. The subsequent `SELECT room_version FROM rooms WHERE room_id = ?` in `_persist_events_txn` (with `search_path=tenant_schema`) finds no row and raises `Exception("Room does not exist ...")`.

The fix direction is no longer "state-groups persistence path forgets tenant context" — it's **"session-level `SET search_path` is unreliable across the Synapse connection pool in this setup"**. Most likely culprit: the per-connection cache in `_set_tenant_schema` (`synapse/storage/database.py:688-705`) assumes session state survives between Twisted adbapi borrows, which isn't always true.

## Observed

- createRoom response: `{"errcode":"M_UNKNOWN","error":"Internal server error"}` — HTTP 500.
- With `SYNAPSE_MT_STRICT_STATE_GROUPS=1` on: the assertion fires at the first `nextval('state_group_id_seq')` call with `search_path=tenant_stress_a_localhost` (correct!) but `tenant=None`. This reveals a SECOND, distinct bug: tenant contextvars don't cross the Twisted thread-pool boundary (see "Finding 2: thread-pool contextvar loss" below).
- With strict mode off: state_groups are correctly persisted (2 rows in `tenant_stress_a_localhost.state_groups`), then `_persist_events_txn` fails because the room metadata was written to `public.rooms`.

## DB state after one failed createRoom (with strict mode off)

```
     src                    | rows | min_id | max_id
----------------------------|------|--------|--------
 public.state_groups        |    0 |    0   |    0
 tenant.state_groups        |    2 |    1   |    2    ← correctly in tenant
 public.state_group_id_seq  |    0 |    0   |    1    ← never advanced (correct)
 tenant.state_group_id_seq  |    0 |    0   |    2    ← advanced by 1 per insert

 public.rooms               |    1                    ← LEAKED: room metadata in public
 tenant.rooms               |    0                    ← but the persist step looks here
```

Contrast with the 2026-04-20 baseline:
```
 public.state_groups           17 rows  ← was the primary symptom
 tenant.state_groups            0 rows
 tenant.state_group_id_seq     20       ← weirdly advanced despite empty table
```

## Diagnosis

The three hypotheses from the design spec are now re-evaluated:

### Hypothesis A (startup writes land in `public`) — **REFUTED**

The boot-time INFO log confirms clean startup state:

```
state_group_id_seq generator initialized at startup (no tenant context active);
  startup_tenant=None startup_max_state_group_id=0 resolved_schema_for_state_groups=public
```

`public.state_groups` was empty both before and after the run. The "17 leaked rows" in the 2026-04-20 baseline were a SYMPTOM of F#3's pre-`/reload` fall-through window (traffic served as `server_name=localhost` accumulated rows in `public.*` tables). Fixing F#3 closed that window, so this hypothesis no longer applies.

### Hypothesis B (`search_path` drops `public`) — **REFUTED** (at least for state_groups)

`_set_tenant_schema` at `synapse/storage/database.py:711` sets `SET search_path TO {schema}` (no `public` fallback). The instrumentation log line at that site shows `search_path=tenant_stress_a_localhost` on every call, and the state_groups writes correctly land in the tenant schema. The "security-first policy" comment in the code is self-consistent and isn't the F#2 root cause.

### Hypothesis C (global `state_groups_persisting` tracker) — **UNEXERCISED**

createRoom fails before the `state_groups_persisting` write path is reached, so there's no evidence for or against this hypothesis in the current repro. It may still be live but is gated behind the new finding below.

### Finding 1 (NEW, primary): `rooms` INSERT lands in `public` despite tenant context being captured

**Evidence:**
- `public.rooms` has the failed attempt's `room_id` (`!KkDaeElOInbMQUHcTO:stress-a.localhost`).
- `tenant_stress_a_localhost.rooms` is empty.
- The `store_room` path at `synapse/storage/databases/main/room.py:172-204` uses `db_pool.simple_insert("rooms", ...)` — an unqualified table name relying on `search_path`.
- Every DB operation logged `Captured tenant context for DB operation: stress-a.localhost` — so `captured_tenant` was correctly propagated into the thread-pool.
- Yet the INSERT went to `public`.

**Likely root cause:** the per-connection cache in `_set_tenant_schema` (`synapse/storage/database.py:687-706`) is stale for at least one connection in the pool. The cache assumes that once we've issued `SET search_path TO <schema>` on a connection, the session-level SET persists across all subsequent borrows of that connection until we issue another `SET`. But the ACTUAL session `search_path` can be reset by any of:
- Explicit `RESET ALL` / `DISCARD ALL` anywhere in the pool lifecycle;
- Connection reconnection (partially handled by `_clear_connection_schema_cache` but only via specific paths);
- pgbouncer behavior on connection release (in `session` mode this should be safe, but worth verifying);
- A bug elsewhere in the codebase that bypasses `_set_tenant_schema` entirely.

The cache at `database.py:688-694` then reports a hit and skips the SET, so the next INSERT runs against whatever `search_path` happens to be on the backend (most likely the default `"$user", public`), and writes to `public.rooms`.

### Finding 2 (NEW, secondary): tenant contextvar is lost inside thread-pool threads

**Evidence:**

```
AssertionError: SYNAPSE_MT_STRICT_STATE_GROUPS: state-groups write with no tenant context.
  search_path=tenant_stress_a_localhost
  target_schema=tenant_stress_a_localhost
  op=nextval state_group_id_seq (full)
```

At the moment of the assertion, the Postgres session is correctly configured for the tenant (`search_path` and `target_schema` both resolve to the tenant), but `get_current_tenant()` returns `None`.

**Mechanism:** `synapse/storage/database.py::runWithConnection` (lines 1193-1270) captures the current tenant in the reactor thread (line 1195), opens a `LoggingContext` in the thread (line 1213), and calls `_set_tenant_schema(conn, captured_tenant)` (line 1262) — but never re-sets the Python `contextvars.ContextVar` for tenant in the thread-pool thread. Python contextvars are per-task/per-thread; they don't propagate when work is dispatched into a Twisted thread pool.

This is NOT the cause of the current createRoom failure (the failure happens because of `search_path` / Finding 1, not contextvar). But it IS a latent bug that could surface later: any code running inside a DB transaction that reads `get_current_tenant()` — for instance, to gate tenant-aware logic in a `txn.call_after` callback, or to build a cache key inside `simple_insert_txn` — gets `None` and takes a non-tenant-aware path. This was already on the radar for the F#1 invalidation follow-ups (see Task 2 code review) and is now confirmed live.

## Fix shape (input to Task 8)

Two related fixes, one critical and one defense-in-depth:

### Fix 1 (critical) — reliable schema routing

Either:

- **1a) Drop the per-connection cache** in `_set_tenant_schema` (`database.py:687-706`). Every `runWithConnection` call re-issues `SET search_path TO <schema>`. Cost: one extra `SET` per DB op (microseconds); eliminates any possibility of cache-coherence bugs.

- **1b) Schema-qualify EVERY INSERT** to tenant-scoped tables explicitly in SQL. Massive code change; tantamount to abandoning the search_path approach. Not viable in this fix's scope.

- **1c) Use `SET LOCAL search_path` inside a transaction** for each runInteraction. Implies explicit `BEGIN` / `COMMIT` management per DB op; may not be compatible with Twisted adbapi's autocommit handling. Worth investigating if 1a has unacceptable perf, otherwise skip.

**Recommendation: 1a.** The cache's stated gain (one-SET-saved-per-borrow) is negligible, and its failure mode is a security-boundary violation (data leak to `public`). This is exactly the kind of cache the plan should never have added.

### Fix 2 (defense in depth) — re-set the contextvar inside the thread pool

In `inner_func` at `database.py:1205-1270`, immediately after setting search_path and before the `func()` call, re-establish the tenant contextvar:

```python
# Re-set tenant contextvar inside the thread pool — contextvars
# don't propagate across the thread boundary.
if captured_tenant is not None:
    ctx_token = set_current_tenant(captured_tenant)
else:
    ctx_token = None

try:
    return func(db_conn, *args, **kwargs)
finally:
    if ctx_token is not None:
        reset_current_tenant(ctx_token)
```

This keeps `get_current_tenant()` correct inside DB transactions. Without this, any tenant-aware logic reached from inside a `runInteraction` callback silently uses the wrong (or no) tenant — exactly the class of latent bugs the Task 4 code review flagged for invalidation call sites.

### Regression tests to add in Task 8

1. **Cross-tenant createRoom integration test** — against a HomeserverTestCase, create rooms on two tenants back-to-back, assert each room's metadata lands in the expected tenant schema (not public).
2. **DB-side assertion of schema isolation** — after one createRoom per tenant, query `public.rooms` and assert it is empty.
3. **Unit test for contextvar propagation into thread pool** — call `db_pool.runInteraction` with a tenant context set in the reactor, assert the transaction callback observes the same tenant via `get_current_tenant()`.

## Log excerpts

### The assertion firing (strict mode on)

```
2026-04-21 13:16:06,966 [server_name=stress-a.localhost] synapse.storage.database - 1197 - INFO -
  Captured tenant context for DB operation: stress-a.localhost (schema: tenant_stress_a_localhost)
2026-04-21 13:16:06,968 [server_name=localhost]          synapse.storage.database - 1197 - INFO -
  Captured tenant context for DB operation: stress-a.localhost (schema: tenant_stress_a_localhost)
2026-04-21 13:16:06,971 [server_name=stress-a.localhost] synapse.http.server - 147 - ERROR -
  Failed handle request via 'RoomCreateRestServlet'
...
  File ".../synapse/storage/databases/state/store.py", line 143, in _mt_state_groups_guard
AssertionError: SYNAPSE_MT_STRICT_STATE_GROUPS: state-groups write with no tenant context.
  search_path=tenant_stress_a_localhost target_schema=tenant_stress_a_localhost
  op=nextval state_group_id_seq (full)
```

### The rooms-lands-in-public failure (strict mode off)

```
2026-04-21 13:17:59,788 [server_name=stress-a.localhost] synapse.http.server - 147 - ERROR -
  Failed handle request via 'RoomCreateRestServlet'
...
  File ".../synapse/storage/databases/main/events.py", line 1089, in _persist_events_txn
    raise Exception(f"Room does not exist {room_id}")
Exception: Room does not exist !KkDaeElOInbMQUHcTO:stress-a.localhost
```

Followed by DB inspection showing `public.rooms` has the row, `tenant.rooms` does not.

## Confidence

- **HIGH** on Finding 1 (rooms writes land in public): directly observable in DB inspection and the "Room does not exist" traceback against the tenant schema.
- **HIGH** on Finding 2 (contextvar lost in thread-pool): the strict-mode assertion triggers with `tenant=None` but correct `search_path`. The lack of contextvar re-set in `inner_func` at `database.py:1205-1270` is visible in the source.
- **MEDIUM-HIGH** on the cache-coherence hypothesis for Finding 1's root cause. Alternative hypotheses (pgbouncer session interaction, a code path bypassing `_set_tenant_schema`, explicit `DISCARD ALL` somewhere) weren't directly tested. Test plan for Task 8: dropping the cache (Fix 1a) and re-running the repro should close Finding 1 — if it doesn't, the real mechanism is elsewhere and further instrumentation at the SQL layer is needed.
