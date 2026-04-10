# Phase 8 — Database Tuning & Connection Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise the tenant ceiling from ~10-15 to ~100-200 by eliminating search_path overhead and adding connection pooling infrastructure.

**Architecture:** Replace the current 3-round-trip search_path pattern (SHOW + SET + restore) with a single session-level SET, add a per-connection schema cache to skip redundant SETs, and add a pgbouncer sidecar with raised pool limits. Each sub-phase is independently shippable.

**Tech Stack:** Python 3.11+, Twisted Trial, psycopg2, pgbouncer, K6 (stress tests), Docker Compose

**Spec:** `docs/superpowers/specs/2026-04-10-phase-8-db-tuning-connection-optimization-design.md`

---

## File structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `synapse/storage/database.py` | Remove SHOW + restore, add connection schema cache |
| Create | `tests/tenant/test_database_tuning.py` | Unit tests for 8a + 8b |
| Modify | `docker-multitenant/config/homeserver.yaml` | Raise `cp_max`, point at pgbouncer |
| Modify | `docker-multitenant/docker-compose.yml` | Add pgbouncer sidecar, raise Postgres max_connections |
| Modify | `docs/multi_tenant.md` | Document capacity curve |

---

## Sub-phase 8a — Eliminate SHOW + restore

### Task 1: Write red probes for 8a

**Files:**
- Create: `tests/tenant/test_database_tuning.py`

- [ ] **Step 1: Write the test file with 8a probes**

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Tests for phase 8 database tuning: search_path optimization and
connection-level schema caching.
"""

import sys
from unittest import TestCase
from unittest.mock import MagicMock, call

# Stub out heavy transitive dependencies before importing database
for _mod in (
    "synapse.logging.opentracing",
    "synapse.metrics",
    "synapse.metrics._reactor_metrics",
    "synapse.metrics.background_process_metrics",
    "synapse.storage.background_updates",
    "synapse.storage.engines",
    "synapse.storage.types",
    "synapse.util",
    "synapse.util.async_helpers",
    "synapse.util.iterutils",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()  # type: ignore[assignment]

from synapse.config.tenants import TenantConfig


def _make_tenant(name: str) -> TenantConfig:
    return TenantConfig(
        server_name=f"{name}.localhost",
        database_schema=f"tenant_{name}",
        signing_key_path=f"/keys/{name}.key",
        media_store_path=f"/media/{name}",
    )


class MockCursor:
    """Tracks every SQL statement executed."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, sql: str, *args: object) -> None:
        self.executed.append(sql)

    def fetchone(self) -> tuple[str]:
        return ("public",)

    def close(self) -> None:
        pass


class MockConnection:
    """Minimal mock of a psycopg2 connection for _set_tenant_schema tests."""

    def __init__(self) -> None:
        self._cursor = MockCursor()

    def cursor(self) -> MockCursor:
        return self._cursor


# ── 8a probes ──────────────────────────────────────────────────────

class TestNoShowSearchPath(TestCase):
    """_set_tenant_schema must NOT execute SHOW search_path."""

    def test_no_show_search_path(self) -> None:
        from synapse.storage.database import DatabasePool

        pool = MagicMock(spec=DatabasePool)
        pool.engine = MagicMock()
        pool.engine.__class__.__name__ = "PostgresEngine"
        # Make isinstance check pass
        from synapse.storage.engines import PostgresEngine
        pool.engine = MagicMock(spec=PostgresEngine)

        conn = MockConnection()
        tenant = _make_tenant("acme")

        # Call the real method on the mock
        DatabasePool._set_tenant_schema(pool, conn, tenant)

        statements = conn._cursor.executed
        for stmt in statements:
            self.assertNotIn(
                "SHOW",
                stmt.upper(),
                f"SHOW search_path should not be issued, got: {stmt}",
            )


class TestNoRestoreMethod(TestCase):
    """_restore_search_path must not exist on DatabasePool."""

    def test_no_restore_search_path(self) -> None:
        from synapse.storage.database import DatabasePool

        self.assertFalse(
            hasattr(DatabasePool, "_restore_search_path"),
            "_restore_search_path should be deleted — restore is no longer needed",
        )


class TestSetTenantSchemaReturnNone(TestCase):
    """_set_tenant_schema must return None (no original to restore)."""

    def test_returns_none(self) -> None:
        from synapse.storage.database import DatabasePool
        from synapse.storage.engines import PostgresEngine

        pool = MagicMock(spec=DatabasePool)
        pool.engine = MagicMock(spec=PostgresEngine)

        conn = MockConnection()
        tenant = _make_tenant("acme")

        result = DatabasePool._set_tenant_schema(pool, conn, tenant)
        self.assertIsNone(result)


class TestSetTenantSchemaIssuesSingleSet(TestCase):
    """_set_tenant_schema must issue exactly one SET statement."""

    def test_single_set(self) -> None:
        from synapse.storage.database import DatabasePool
        from synapse.storage.engines import PostgresEngine

        pool = MagicMock(spec=DatabasePool)
        pool.engine = MagicMock(spec=PostgresEngine)

        conn = MockConnection()
        tenant = _make_tenant("acme")

        DatabasePool._set_tenant_schema(pool, conn, tenant)

        statements = conn._cursor.executed
        set_stmts = [s for s in statements if s.upper().startswith("SET")]
        self.assertEqual(
            len(set_stmts),
            1,
            f"Expected exactly 1 SET statement, got {len(set_stmts)}: {set_stmts}",
        )
        self.assertIn("tenant_acme", set_stmts[0])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/monta/Documents/workspace/synapse-multitenant && python -m pytest tests/tenant/test_database_tuning.py -v 2>&1 | tail -20`

Expected: Failures — `_set_tenant_schema` still issues SHOW and returns the original search_path; `_restore_search_path` still exists.

- [ ] **Step 3: Commit red probes**

```bash
git add tests/tenant/test_database_tuning.py
git commit -m "test(phase-8a): add red probes for search_path optimization

4 probes: no SHOW, no _restore_search_path method, returns None,
single SET statement.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

### Task 2: Simplify `_set_tenant_schema` — remove SHOW + change return type

**Files:**
- Modify: `synapse/storage/database.py:657-702`

- [ ] **Step 1: Rewrite `_set_tenant_schema` to remove SHOW and return None**

Replace `synapse/storage/database.py` lines 657-702 with:

```python
    def _set_tenant_schema(
        self, conn: Connection, tenant: "TenantConfig"
    ) -> None:
        """Set the PostgreSQL search_path to the tenant's schema.

        Uses a session-level SET (not SET LOCAL) so it works in both
        transactional and autocommit modes. The connection returns to the
        pool with this schema still set — the next borrower will SET to
        its own tenant.

        Args:
            conn: The database connection.
            tenant: The tenant configuration.
        """
        if not isinstance(self.engine, PostgresEngine):
            return

        schema = tenant.database_schema
        if not schema.replace("_", "").isalnum():
            raise ValueError(f"Invalid schema name: {schema}")

        cursor = conn.cursor()
        try:
            # Security-first policy: do NOT include `, public` as a fallback.
            # Any table missing from the tenant schema must fail loud, not
            # silently resolve in public. See
            # docs/multi_tenant_isolation_model.md for the reasoning.
            cursor.execute(f"SET search_path TO {schema}")
            logger.debug(
                "Set search_path to '%s' for tenant %s",
                schema,
                tenant.server_name,
            )
        finally:
            cursor.close()
```

- [ ] **Step 2: Run the 8a probes to check progress**

Run: `cd /home/monta/Documents/workspace/synapse-multitenant && python -m pytest tests/tenant/test_database_tuning.py -v 2>&1 | tail -20`

Expected: `test_no_show_search_path` PASS, `test_single_set` PASS, `test_returns_none` PASS. `test_no_restore_search_path` still FAILS (method exists).

### Task 3: Delete `_restore_search_path` and update `runWithConnection`

**Files:**
- Modify: `synapse/storage/database.py:758-773` (delete)
- Modify: `synapse/storage/database.py:1228-1268` (simplify)

- [ ] **Step 1: Delete `_restore_search_path` method**

Remove lines 758-773 entirely (the full `_restore_search_path` method).

- [ ] **Step 2: Simplify `runWithConnection` inner_func**

In `runWithConnection` (around line 1228 after deletion shift), replace the try/finally block. The current code:

```python
                    original_search_path: str | None = None

                    try:
                        if db_autocommit:
                            self.engine.attempt_to_set_autocommit(conn, True)
                        if isolation_level is not None:
                            self.engine.attempt_to_set_isolation_level(
                                conn, isolation_level
                            )

                        # Set tenant schema if multi-tenant mode is active
                        if captured_tenant is not None and isinstance(self.engine, PostgresEngine):
                            original_search_path = self._set_tenant_schema(
                                conn, captured_tenant
                            )
                            logger.info(
                                "Set database schema for tenant %s: %s",
                                captured_tenant.server_name,
                                captured_tenant.database_schema,
                            )

                        db_conn = LoggingDatabaseConnection(
                            conn=conn,
                            engine=self.engine,
                            default_txn_name="runWithConnection",
                            server_name=self.server_name,
                        )
                        return func(db_conn, *args, **kwargs)
                    finally:
                        # Restore original search_path if we changed it
                        if original_search_path is not None and isinstance(
                            self.engine, PostgresEngine
                        ):
                            self._restore_search_path(conn, original_search_path)

                        if db_autocommit:
                            self.engine.attempt_to_set_autocommit(conn, False)
                        if isolation_level:
                            self.engine.attempt_to_set_isolation_level(conn, None)
```

Replace with:

```python
                    try:
                        if db_autocommit:
                            self.engine.attempt_to_set_autocommit(conn, True)
                        if isolation_level is not None:
                            self.engine.attempt_to_set_isolation_level(
                                conn, isolation_level
                            )

                        # Set tenant schema if multi-tenant mode is active.
                        # Session-level SET persists across transactions on this
                        # connection — no restore needed on exit.
                        if captured_tenant is not None and isinstance(self.engine, PostgresEngine):
                            self._set_tenant_schema(conn, captured_tenant)

                        db_conn = LoggingDatabaseConnection(
                            conn=conn,
                            engine=self.engine,
                            default_txn_name="runWithConnection",
                            server_name=self.server_name,
                        )
                        return func(db_conn, *args, **kwargs)
                    finally:
                        if db_autocommit:
                            self.engine.attempt_to_set_autocommit(conn, False)
                        if isolation_level:
                            self.engine.attempt_to_set_isolation_level(conn, None)
```

Key changes: removed `original_search_path` variable, removed the restore call in `finally`, removed the info-level log (the debug log inside `_set_tenant_schema` is sufficient).

- [ ] **Step 3: Run all 8a probes**

Run: `cd /home/monta/Documents/workspace/synapse-multitenant && python -m pytest tests/tenant/test_database_tuning.py -v 2>&1 | tail -20`

Expected: All 4 probes PASS.

- [ ] **Step 4: Run existing tenant tests to check for regressions**

Run: `cd /home/monta/Documents/workspace/synapse-multitenant && python -m pytest tests/tenant/ -v 2>&1 | tail -30`

Expected: All existing tenant tests PASS.

- [ ] **Step 5: Commit**

```bash
git add synapse/storage/database.py tests/tenant/test_database_tuning.py
git commit -m "feat(phase-8a): eliminate SHOW + restore from search_path switching

Replace 3-round-trip pattern (SHOW + SET + restore) with a single
session-level SET. Works in both transactional and autocommit modes.
Delete _restore_search_path entirely. 3 SQL commands → 1 per
runWithConnection call.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Sub-phase 8b — Connection-level schema caching

### Task 4: Write red probes for 8b

**Files:**
- Modify: `tests/tenant/test_database_tuning.py`

- [ ] **Step 1: Add 8b probes to the test file**

Append to `tests/tenant/test_database_tuning.py`:

```python
# ── 8b probes ──────────────────────────────────────────────────────

class TestCacheHitSkipsSet(TestCase):
    """Calling _set_tenant_schema twice with the same tenant on the same
    connection should issue SET only once."""

    def test_cache_hit_skips_set(self) -> None:
        from synapse.storage.database import DatabasePool
        from synapse.storage.engines import PostgresEngine

        pool = MagicMock(spec=DatabasePool)
        pool.engine = MagicMock(spec=PostgresEngine)
        pool._connection_schemas = {}

        conn = MockConnection()
        tenant = _make_tenant("acme")

        # First call — should SET
        DatabasePool._set_tenant_schema(pool, conn, tenant)
        # Second call — should skip (cache hit)
        DatabasePool._set_tenant_schema(pool, conn, tenant)

        statements = conn._cursor.executed
        set_stmts = [s for s in statements if s.upper().startswith("SET")]
        self.assertEqual(
            len(set_stmts),
            1,
            f"Expected 1 SET (second call cached), got {len(set_stmts)}: {set_stmts}",
        )


class TestCacheMissOnTenantSwitch(TestCase):
    """Switching tenants on the same connection should issue SET twice."""

    def test_cache_miss_on_switch(self) -> None:
        from synapse.storage.database import DatabasePool
        from synapse.storage.engines import PostgresEngine

        pool = MagicMock(spec=DatabasePool)
        pool.engine = MagicMock(spec=PostgresEngine)
        pool._connection_schemas = {}

        conn = MockConnection()
        tenant_a = _make_tenant("acme")
        tenant_b = _make_tenant("corp")

        DatabasePool._set_tenant_schema(pool, conn, tenant_a)
        DatabasePool._set_tenant_schema(pool, conn, tenant_b)

        statements = conn._cursor.executed
        set_stmts = [s for s in statements if s.upper().startswith("SET")]
        self.assertEqual(
            len(set_stmts),
            2,
            f"Expected 2 SETs (different tenants), got {len(set_stmts)}: {set_stmts}",
        )
        self.assertIn("tenant_acme", set_stmts[0])
        self.assertIn("tenant_corp", set_stmts[1])


class TestCacheInvalidationOnReconnect(TestCase):
    """After conn.reconnect(), the cache entry must be cleared so the
    next _set_tenant_schema issues a fresh SET."""

    def test_reconnect_clears_cache(self) -> None:
        from synapse.storage.database import DatabasePool
        from synapse.storage.engines import PostgresEngine

        pool = MagicMock(spec=DatabasePool)
        pool.engine = MagicMock(spec=PostgresEngine)
        pool._connection_schemas = {}

        conn = MockConnection()
        tenant = _make_tenant("acme")

        # First call sets cache
        DatabasePool._set_tenant_schema(pool, conn, tenant)
        self.assertIn(id(conn), pool._connection_schemas)

        # Simulate what runWithConnection does after reconnect:
        # it should clear the cache entry
        DatabasePool._clear_connection_schema_cache(pool, conn)
        self.assertNotIn(id(conn), pool._connection_schemas)

        # Next SET should execute (cache miss)
        conn._cursor.executed.clear()
        DatabasePool._set_tenant_schema(pool, conn, tenant)
        set_stmts = [s for s in conn._cursor.executed if s.upper().startswith("SET")]
        self.assertEqual(len(set_stmts), 1, "SET should fire after cache clear")
```

- [ ] **Step 2: Run 8b probes to verify they fail**

Run: `cd /home/monta/Documents/workspace/synapse-multitenant && python -m pytest tests/tenant/test_database_tuning.py::TestCacheHitSkipsSet tests/tenant/test_database_tuning.py::TestCacheMissOnTenantSwitch tests/tenant/test_database_tuning.py::TestCacheInvalidationOnReconnect -v 2>&1 | tail -20`

Expected: All 3 FAIL — `_connection_schemas` doesn't exist, no caching logic, `_clear_connection_schema_cache` doesn't exist.

- [ ] **Step 3: Commit red probes**

```bash
git add tests/tenant/test_database_tuning.py
git commit -m "test(phase-8b): add red probes for connection schema caching

3 probes: cache hit skips SET, cache miss on tenant switch issues SET,
reconnect clears cache entry.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

### Task 5: Add connection schema cache to DatabasePool

**Files:**
- Modify: `synapse/storage/database.py`

- [ ] **Step 1: Add `_connection_schemas` dict to `DatabasePool.__init__`**

In `DatabasePool.__init__` (around line 624, after `self.engine = engine`), add:

```python
        # Phase 8b: per-connection schema cache.  Maps id(conn) → schema
        # name currently SET on that connection.  Avoids redundant SET
        # commands for consecutive same-tenant requests on the same conn.
        self._connection_schemas: dict[int, str] = {}
```

- [ ] **Step 2: Add `_clear_connection_schema_cache` method**

Add after `_set_tenant_schema` (after the closing of that method):

```python
    def _clear_connection_schema_cache(self, conn: Connection) -> None:
        """Remove the cached schema for a connection.

        Called after conn.reconnect() — the underlying DB connection is
        fresh and its search_path is back to the default.
        """
        self._connection_schemas.pop(id(conn), None)
```

- [ ] **Step 3: Add caching logic to `_set_tenant_schema`**

Update `_set_tenant_schema` to check the cache before issuing SET. Replace the method body (keeping the signature `def _set_tenant_schema(self, conn: Connection, tenant: "TenantConfig") -> None:`):

```python
    def _set_tenant_schema(
        self, conn: Connection, tenant: "TenantConfig"
    ) -> None:
        """Set the PostgreSQL search_path to the tenant's schema.

        Uses a session-level SET (not SET LOCAL) so it works in both
        transactional and autocommit modes. The connection returns to the
        pool with this schema still set — the next borrower will SET to
        its own tenant.

        Includes connection-level caching: if this connection already has
        the requested schema set, the SET is skipped entirely.

        Args:
            conn: The database connection.
            tenant: The tenant configuration.
        """
        if not isinstance(self.engine, PostgresEngine):
            return

        schema = tenant.database_schema
        if not schema.replace("_", "").isalnum():
            raise ValueError(f"Invalid schema name: {schema}")

        # Cache check: skip SET if this connection already has the right schema
        conn_id = id(conn)
        if self._connection_schemas.get(conn_id) == schema:
            logger.debug(
                "search_path cache hit for '%s' on conn %d",
                schema,
                conn_id,
            )
            return

        cursor = conn.cursor()
        try:
            # Security-first policy: do NOT include `, public` as a fallback.
            cursor.execute(f"SET search_path TO {schema}")
            self._connection_schemas[conn_id] = schema
            logger.debug(
                "Set search_path to '%s' for tenant %s (conn %d)",
                schema,
                tenant.server_name,
                conn_id,
            )
        finally:
            cursor.close()
```

- [ ] **Step 4: Clear cache after reconnect in `runWithConnection`**

In `runWithConnection`'s `inner_func`, after each `conn.reconnect()` call (there are two sites — the txn_limit reconnect and the closed-connection reconnect), add a cache-clear call. After line ~1215 (`conn.reconnect()` for txn limit):

```python
                            conn.reconnect()
                            self._clear_connection_schema_cache(conn)
```

And after line ~1223 (`conn.reconnect()` for closed connection):

```python
                        conn.reconnect()
                        self._clear_connection_schema_cache(conn)
```

- [ ] **Step 5: Run all probes (8a + 8b)**

Run: `cd /home/monta/Documents/workspace/synapse-multitenant && python -m pytest tests/tenant/test_database_tuning.py -v 2>&1 | tail -25`

Expected: All 7 probes PASS (4 from 8a + 3 from 8b).

- [ ] **Step 6: Run full tenant test suite**

Run: `cd /home/monta/Documents/workspace/synapse-multitenant && python -m pytest tests/tenant/ -v 2>&1 | tail -30`

Expected: All existing tests PASS.

- [ ] **Step 7: Commit**

```bash
git add synapse/storage/database.py tests/tenant/test_database_tuning.py
git commit -m "feat(phase-8b): add connection-level schema caching

Track which schema each connection has SET. Skip redundant SET commands
for consecutive same-tenant requests. Cache cleared on reconnect.
Bursty same-tenant traffic: 1 SQL command → 0.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Sub-phase 8c — Pool tuning + pgbouncer

### Task 6: Raise cp_max and add pgbouncer sidecar

**Files:**
- Modify: `docker-multitenant/config/homeserver.yaml:68-69`
- Modify: `docker-multitenant/docker-compose.yml`

- [ ] **Step 1: Update homeserver.yaml pool config**

In `docker-multitenant/config/homeserver.yaml`, replace:

```yaml
    cp_min: 5
    cp_max: 10
```

with:

```yaml
    cp_min: 5
    cp_max: 50
```

- [ ] **Step 2: Update homeserver.yaml to connect through pgbouncer**

In `docker-multitenant/config/homeserver.yaml`, replace:

```yaml
    host: postgres
    port: 5432
```

with:

```yaml
    host: pgbouncer
    port: 6432
```

- [ ] **Step 3: Add pgbouncer service to docker-compose.yml**

In `docker-multitenant/docker-compose.yml`, add the pgbouncer service after the `postgres` service block (before `keygen`):

```yaml
  # PgBouncer connection pooler
  pgbouncer:
    image: edoburu/pgbouncer:1.23.1
    container_name: synapse-mt-pgbouncer
    environment:
      DATABASE_URL: postgres://synapse:synapse_password@postgres:5432/synapse_multitenant
      POOL_MODE: session
      DEFAULT_POOL_SIZE: 50
      MAX_CLIENT_CONN: 200
      MAX_DB_CONNECTIONS: 100
      AUTH_TYPE: plain
    ports:
      - "16432:6432"
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -p 6432 -U synapse || true"]
      interval: 5s
      timeout: 5s
      retries: 10
    networks:
      - synapse-net
```

- [ ] **Step 4: Raise Postgres max_connections**

In `docker-multitenant/docker-compose.yml`, add a `command` to the postgres service to raise max_connections:

```yaml
  postgres:
    image: postgres:15-alpine
    container_name: synapse-mt-postgres
    command: ["postgres", "-c", "max_connections=200"]
```

- [ ] **Step 5: Update Synapse depends_on to include pgbouncer**

In `docker-multitenant/docker-compose.yml`, update the `synapse` service's `depends_on` to include pgbouncer:

```yaml
    depends_on:
      postgres:
        condition: service_healthy
      pgbouncer:
        condition: service_healthy
      keygen:
        condition: service_completed_successfully
```

- [ ] **Step 6: Also update `init-schemas` service to connect directly to postgres**

The `init-schemas` service must still connect directly to Postgres (not through pgbouncer) since it runs DDL. Verify that its environment uses `POSTGRES_HOST: postgres` and `POSTGRES_PORT: "5432"` — these should already be correct. No change needed.

- [ ] **Step 7: Commit**

```bash
git add docker-multitenant/config/homeserver.yaml docker-multitenant/docker-compose.yml
git commit -m "feat(phase-8c): add pgbouncer sidecar and raise pool limits

pgbouncer in session mode (safe with session-level SET search_path).
cp_max raised from 10 to 50. Postgres max_connections raised to 200.
Synapse connects through pgbouncer on port 6432.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

### Task 7: Document capacity curve

**Files:**
- Modify: `docs/multi_tenant.md`

- [ ] **Step 1: Find the performance section in multi_tenant.md**

Run: `grep -n -i 'performance\|capacity\|scaling\|connection pool' docs/multi_tenant.md | head -10`

If no performance section exists, add one at the end.

- [ ] **Step 2: Add capacity documentation**

Append a capacity section to `docs/multi_tenant.md`:

```markdown
## Connection pool & capacity

### Search path switching

Each database operation sets the PostgreSQL `search_path` to the active
tenant's schema. Phase 8 optimized this from 3 SQL commands per call
(SHOW + SET + restore) to at most 1 (session-level SET), with a
connection-level cache that skips the SET entirely when the same tenant
reuses the connection.

### Pool configuration

The shared connection pool (`cp_max` in `homeserver.yaml`) is sized to
serve all tenants concurrently. Recommended values:

| Tenants | `cp_max` | Notes |
|---------|----------|-------|
| 1-10 | 10 | Default; direct Postgres connection is fine |
| 10-50 | 50 | Add pgbouncer for connection multiplexing |
| 50-200 | 50-100 | pgbouncer required; raise Postgres `max_connections` |

### Capacity estimates

| Configuration | Tenant ceiling | Limiting factor |
|--------------|----------------|----------------|
| Default (`cp_max=10`, no pgbouncer) | ~10-15 | Pool starvation |
| Pool tuned (`cp_max=50`) | ~30-50 | GIL + SET overhead |
| + schema caching | ~50-100 | GIL + rate limiter memory |
| + pgbouncer (session mode) | ~100-200 | GIL (hard ceiling) |

The GIL ceiling is addressed by worker support (phase 12).

### pgbouncer

The `docker-multitenant/` demo includes a pgbouncer sidecar in session
mode. Session mode pins connections for the client session duration,
which is required because Synapse uses session-level `SET search_path`.
Transaction mode is **not compatible** — it would reset `search_path`
between transactions.

pgbouncer settings in the demo:
- `POOL_MODE=session`
- `DEFAULT_POOL_SIZE=50`
- `MAX_CLIENT_CONN=200`
- `MAX_DB_CONNECTIONS=100`
```

- [ ] **Step 3: Commit**

```bash
git add docs/multi_tenant.md
git commit -m "docs: add capacity curve and pool tuning guidance

Documents search_path optimization, pool sizing recommendations,
capacity estimates by configuration, and pgbouncer session mode
requirement.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

### Task 8: Update roadmap to reflect SET LOCAL → session SET change

**Files:**
- Modify: `docs/multi_tenant_roadmap.md:423-429`

- [ ] **Step 1: Update roadmap phase 8a description**

In `docs/multi_tenant_roadmap.md`, replace the 8a description:

```
   **8a — `SET LOCAL` search_path.** Replace the current 3-round-trip
   pattern (`SHOW search_path` → `SET search_path` → `SET` restore)
   with `SET LOCAL search_path TO <schema>`, which scopes to the
   transaction and auto-resets on commit/rollback. Eliminates
   `_restore_search_path` entirely. ~10 lines in
   `synapse/storage/database.py`. Impact: ~33% fewer DB round trips
   per transaction.
```

with:

```
   **8a — Session-level `SET` search_path.** Replace the current
   3-round-trip pattern (`SHOW search_path` → `SET search_path` →
   `SET` restore) with a single session-level
   `SET search_path TO <schema>`. Session-level SET was chosen over
   `SET LOCAL` because Synapse has many `db_autocommit=True` code paths
   where `SET LOCAL` would scope to just the SET statement itself.
   Eliminates `_restore_search_path` entirely. ~10 lines in
   `synapse/storage/database.py`. Impact: ~67% fewer DB round trips
   per transaction.
```

- [ ] **Step 2: Commit**

```bash
git add docs/multi_tenant_roadmap.md
git commit -m "docs: update roadmap phase 8a — SET LOCAL → session SET

SET LOCAL is incompatible with db_autocommit=True paths. Session-level
SET works in both modes and enables clean connection caching.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Definition of done

- [ ] All 7 unit probes green (4 from 8a, 3 from 8b)
- [ ] All existing `tests/tenant/` tests still pass
- [ ] `_restore_search_path` deleted from `database.py`
- [ ] `SHOW search_path` no longer appears in `database.py`
- [ ] `_connection_schemas` cache dict on `DatabasePool`, cleared on reconnect
- [ ] pgbouncer sidecar in `docker-multitenant/docker-compose.yml`
- [ ] `cp_max` raised to 50, Postgres `max_connections` raised to 200
- [ ] Capacity curve documented in `docs/multi_tenant.md`
- [ ] Roadmap updated to reflect session SET over SET LOCAL
- [ ] `/sync-roadmap` run after completion
