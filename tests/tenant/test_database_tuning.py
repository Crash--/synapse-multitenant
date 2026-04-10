#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Tests for phase 8 database tuning: search_path optimization and
connection-level schema caching.
"""

from unittest import TestCase
from unittest.mock import MagicMock

from synapse.config.tenants import TenantConfig
from synapse.storage.database import DatabasePool
from synapse.storage.engines import PostgresEngine


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
        pool = MagicMock(spec=DatabasePool)
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
        self.assertFalse(
            hasattr(DatabasePool, "_restore_search_path"),
            "_restore_search_path should be deleted — restore is no longer needed",
        )


class TestSetTenantSchemaReturnNone(TestCase):
    """_set_tenant_schema must return None (no original to restore)."""

    def test_returns_none(self) -> None:
        pool = MagicMock(spec=DatabasePool)
        pool.engine = MagicMock(spec=PostgresEngine)

        conn = MockConnection()
        tenant = _make_tenant("acme")

        result = DatabasePool._set_tenant_schema(pool, conn, tenant)
        self.assertIsNone(result)


class TestSetTenantSchemaIssuesSingleSet(TestCase):
    """_set_tenant_schema must issue exactly one SET statement."""

    def test_single_set(self) -> None:
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


# ── 8b probes ──────────────────────────────────────────────────────

class TestCacheHitSkipsSet(TestCase):
    """Calling _set_tenant_schema twice with the same tenant on the same
    connection should issue SET only once."""

    def test_cache_hit_skips_set(self) -> None:
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
