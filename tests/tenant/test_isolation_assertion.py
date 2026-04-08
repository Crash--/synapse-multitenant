#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#

"""
Unit tests for `DatabasePool.assert_tenant_schema_isolated`.

These tests poke the unbound method directly with a tiny stand-in for
`DatabasePool` so we don't have to spin up a real connection pool.
"""

from unittest import TestCase
from unittest.mock import MagicMock

from synapse.storage.database import DatabasePool
from synapse.storage.engines.postgres import PostgresEngine


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class _FakePool:
    """Just enough of `DatabasePool` to satisfy `assert_tenant_schema_isolated`."""

    def __init__(self, engine):
        self.engine = engine


def _make_tenant(server_name="acme.localhost", database_schema="tenant_acme"):
    tenant = MagicMock()
    tenant.server_name = server_name
    tenant.database_schema = database_schema
    return tenant


class AssertTenantSchemaIsolatedTestCase(TestCase):
    def test_raises_when_tables_missing(self):
        cursor = _FakeCursor(rows=[("events",), ("rooms",)])
        conn = _FakeConn(cursor)
        pool = _FakePool(engine=MagicMock(spec=PostgresEngine))
        tenant = _make_tenant()
        expected = ["events", "rooms", "users", "profiles", "devices"]

        with self.assertRaises(RuntimeError) as ctx:
            DatabasePool.assert_tenant_schema_isolated(
                pool, conn, tenant, expected
            )

        msg = str(ctx.exception)
        # Missing tables surface in the error.
        self.assertIn("users", msg)
        self.assertIn("profiles", msg)
        self.assertIn("devices", msg)
        # Tenant identity surfaces in the error.
        self.assertIn("acme.localhost", msg)
        self.assertIn("tenant_acme", msg)
        # Doc reference is preserved for operators.
        self.assertIn("docs/multi_tenant_isolation_model.md", msg)

    def test_does_not_raise_when_all_tables_present(self):
        expected = ["events", "rooms", "users"]
        cursor = _FakeCursor(rows=[(t,) for t in expected])
        conn = _FakeConn(cursor)
        pool = _FakePool(engine=MagicMock(spec=PostgresEngine))
        tenant = _make_tenant()

        # Should not raise.
        DatabasePool.assert_tenant_schema_isolated(pool, conn, tenant, expected)

    def test_skips_when_engine_is_not_postgres(self):
        # Engine is not a PostgresEngine -> early return regardless of cursor.
        cursor = _FakeCursor(rows=[])
        conn = _FakeConn(cursor)
        pool = _FakePool(engine=MagicMock())  # not PostgresEngine
        tenant = _make_tenant()

        # Should not raise even though no tables are present.
        DatabasePool.assert_tenant_schema_isolated(
            pool, conn, tenant, ["events", "rooms"]
        )
        # And no SQL was executed against the cursor.
        self.assertEqual(cursor.executed, [])

    def test_truncates_sample_to_ten_with_indicator(self):
        cursor = _FakeCursor(rows=[])
        conn = _FakeConn(cursor)
        pool = _FakePool(engine=MagicMock(spec=PostgresEngine))
        tenant = _make_tenant()
        # 12 missing tables -> sample truncated to 10 with "..." indicator.
        expected = [f"t_{i:02d}" for i in range(12)]

        with self.assertRaises(RuntimeError) as ctx:
            DatabasePool.assert_tenant_schema_isolated(
                pool, conn, tenant, expected
            )

        msg = str(ctx.exception)
        self.assertIn("...", msg)
        # First ten (sorted) appear; last two do not.
        for i in range(10):
            self.assertIn(f"t_{i:02d}", msg)
        self.assertNotIn("t_10", msg)
        self.assertNotIn("t_11", msg)
