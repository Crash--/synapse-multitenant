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
Unit tests for the tenant schema bootstrap SQL generator.

These tests cover the pure-function SQL builder, not the end-to-end
`create_tenant_schema` script. They run without a live database by
feeding synthetic `(table_name, column_name, sequence_name)` tuples
and asserting on the emitted SQL.
"""

from unittest import TestCase

from scripts.create_tenant_schema import build_clone_schema_sql


class BuildCloneSchemaSqlTestCase(TestCase):
    def test_emits_create_sequence_per_tenant(self):
        sql_statements = build_clone_schema_sql(
            target_schema="tenant_acme",
            tables=["events", "profiles"],
            sequences=["events_stream_seq", "user_id_seq"],
            sequence_defaults=[
                ("events", "stream_ordering", "events_stream_seq"),
                ("users", "id", "user_id_seq"),
            ],
        )
        joined = "\n".join(sql_statements)
        self.assertIn("CREATE SEQUENCE", joined)
        self.assertIn("tenant_acme.events_stream_seq", joined)
        self.assertIn("tenant_acme.user_id_seq", joined)

    def test_emits_create_table_like_per_table(self):
        sql_statements = build_clone_schema_sql(
            target_schema="tenant_acme",
            tables=["events", "profiles"],
            sequences=[],
            sequence_defaults=[],
        )
        joined = "\n".join(sql_statements)
        self.assertIn(
            "CREATE TABLE tenant_acme.events (LIKE public.events INCLUDING ALL)",
            joined,
        )
        self.assertIn(
            "CREATE TABLE tenant_acme.profiles (LIKE public.profiles INCLUDING ALL)",
            joined,
        )

    def test_repoints_column_defaults_to_tenant_sequence(self):
        sql_statements = build_clone_schema_sql(
            target_schema="tenant_acme",
            tables=["events"],
            sequences=["events_stream_seq"],
            sequence_defaults=[
                ("events", "stream_ordering", "events_stream_seq"),
            ],
        )
        joined = "\n".join(sql_statements)
        self.assertIn(
            "ALTER TABLE tenant_acme.events "
            "ALTER COLUMN stream_ordering "
            "SET DEFAULT nextval('tenant_acme.events_stream_seq')",
            joined,
        )

    def test_create_sequence_before_create_table(self):
        """Sequences must be created before tables, and ALTER defaults after."""
        sql_statements = build_clone_schema_sql(
            target_schema="tenant_acme",
            tables=["events"],
            sequences=["events_stream_seq"],
            sequence_defaults=[
                ("events", "stream_ordering", "events_stream_seq"),
            ],
        )
        seq_idx = next(
            i for i, s in enumerate(sql_statements) if "CREATE SEQUENCE" in s
        )
        tbl_idx = next(
            i for i, s in enumerate(sql_statements) if "CREATE TABLE" in s
        )
        alt_idx = next(
            i for i, s in enumerate(sql_statements) if "ALTER TABLE" in s
        )
        self.assertLess(seq_idx, tbl_idx)
        self.assertLess(tbl_idx, alt_idx)

    def test_rejects_invalid_schema_name(self):
        """Schema names are validated at every call site; the builder is one."""
        with self.assertRaises(ValueError) as ctx:
            build_clone_schema_sql(
                target_schema="tenant_acme; DROP TABLE users; --",
                tables=["events"],
                sequences=[],
                sequence_defaults=[],
            )
        self.assertIn("Invalid schema name", str(ctx.exception))
