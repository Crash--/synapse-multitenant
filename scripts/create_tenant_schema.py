#!/usr/bin/env python3
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
Script to create and initialize a new tenant schema in PostgreSQL.

This script creates a new PostgreSQL schema for a tenant and runs all
Synapse database migrations within that schema. It should be run before
a new tenant can use the multi-tenant Synapse deployment.

Usage:
    python -m scripts.create_tenant_schema \
        --config-path /path/to/homeserver.yaml \
        --tenant-name acme.com

    Or to create all schemas defined in config:
    python -m scripts.create_tenant_schema \
        --config-path /path/to/homeserver.yaml \
        --all-tenants
"""

import argparse
import logging
import os
import sys
from typing import Optional

import psycopg2
import yaml

# Add the synapse directory to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from synapse.config.tenants import TenantConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def load_config(config_path: str) -> dict:
    """Load the Synapse configuration file."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def get_db_connection_params(config: dict) -> dict:
    """Extract database connection parameters from Synapse config."""
    db_config = config.get("database", {})

    # Handle both new and old config formats
    if "args" in db_config:
        args = db_config["args"]
    else:
        # Try to extract from name-based config
        args = {}
        if "name" in db_config and db_config["name"] == "psycopg2":
            for key in ["user", "password", "database", "host", "port"]:
                if key in db_config:
                    args[key] = db_config[key]

    return {
        "host": args.get("host", "localhost"),
        "port": args.get("port", 5432),
        "user": args.get("user", "synapse"),
        "password": args.get("password", ""),
        "database": args.get("database", args.get("dbname", "synapse")),
    }


def schema_exists(conn, schema_name: str) -> bool:
    """Check if a schema already exists."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = %s)",
            (schema_name,),
        )
        return cur.fetchone()[0]


def create_schema(conn, schema_name: str) -> None:
    """Create a new PostgreSQL schema."""
    # Validate schema name to prevent SQL injection
    if not schema_name.replace("_", "").isalnum():
        raise ValueError(f"Invalid schema name: {schema_name}")

    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_name}")
        logger.info(f"Created schema: {schema_name}")


def build_clone_schema_sql(
    target_schema: str,
    tables: list,
    sequences: list,
    sequence_defaults: list,
    source_schema: str = "public",
) -> list:
    """Build the SQL statements that clone a schema with per-tenant sequences.

    Pure-function SQL generator -- does not touch the DB. Returns an
    ordered list of SQL statements that, when executed, produce a
    tenant schema containing:

      1. Per-tenant copies of every sequence in ``sequences``.
      2. Cloned copies of every table in ``tables``, via LIKE INCLUDING ALL.
      3. ALTER statements repointing each (table, column) default to
         the per-tenant sequence copy.

    Args:
        target_schema: Tenant schema name (validated).
        tables: Names of tables to clone.
        sequences: Names of sequences to clone.
        sequence_defaults: ``(table, column, sequence_name)`` tuples.
        source_schema: Schema to clone from. Defaults to ``"public"``.

    Returns:
        Ordered list of SQL statements.

    Raises:
        ValueError: on any name that fails the alphanumeric+underscore
            safety check.
    """

    def _safe(name: str) -> str:
        if not name.replace("_", "").isalnum():
            raise ValueError(f"Invalid schema name: {name}")
        return name

    target = _safe(target_schema)
    source = _safe(source_schema)

    statements: list = []

    # 1. Sequences -- per-tenant copies starting at 1 (postgres default).
    #    Tenant tables start empty, so no seeding needed.
    for seq in sequences:
        s = _safe(seq)
        statements.append(f"CREATE SEQUENCE IF NOT EXISTS {target}.{s}")

    # 2. Tables -- LIKE INCLUDING ALL clones structure, indexes,
    #    constraints, and defaults (the defaults still point at
    #    source_schema sequences at this point).
    for tbl in tables:
        t = _safe(tbl)
        statements.append(
            f"CREATE TABLE {target}.{t} "
            f"(LIKE {source}.{t} INCLUDING ALL)"
        )

    # 3. Repoint column defaults to per-tenant sequence copies.
    for tbl, col, seq in sequence_defaults:
        t = _safe(tbl)
        c = _safe(col)
        s = _safe(seq)
        statements.append(
            f"ALTER TABLE {target}.{t} "
            f"ALTER COLUMN {c} "
            f"SET DEFAULT nextval('{target}.{s}')"
        )

    return statements


def discover_schema_surface(
    conn,
    source_schema: str = "public",
):
    """Discover tables, sequences, and sequence/column bindings.

    Queries PostgreSQL catalogs to enumerate:
      - Every BASE TABLE in ``source_schema``.
      - Every sequence in ``source_schema``.
      - Every column whose default is ``nextval('<source_schema>.<seq>')``.

    Returns:
        ``(tables, sequences, sequence_defaults)`` -- the three inputs to
        :func:`build_clone_schema_sql`.
    """
    import re

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = %s
               AND table_type = 'BASE TABLE'
             ORDER BY table_name
            """,
            (source_schema,),
        )
        tables = [row[0] for row in cur.fetchall()]

        cur.execute(
            """
            SELECT sequence_name
              FROM information_schema.sequences
             WHERE sequence_schema = %s
             ORDER BY sequence_name
            """,
            (source_schema,),
        )
        sequences = [row[0] for row in cur.fetchall()]

        cur.execute(
            """
            SELECT c.table_name, c.column_name, c.column_default
              FROM information_schema.columns c
             WHERE c.table_schema = %s
               AND c.column_default LIKE 'nextval(%%'
             ORDER BY c.table_name, c.column_name
            """,
            (source_schema,),
        )
        sequence_defaults = []
        for table_name, column_name, column_default in cur.fetchall():
            # column_default looks like:
            #   nextval('public.events_stream_seq'::regclass)
            match = re.search(
                r"nextval\('(?:[^.]+\.)?([^']+)'", column_default
            )
            if match:
                seq_name = match.group(1)
                sequence_defaults.append(
                    (table_name, column_name, seq_name)
                )

        return tables, sequences, sequence_defaults


def clone_schema_with_sequences(
    conn,
    source_schema: str,
    target_schema: str,
) -> None:
    """Clone a schema into a tenant schema with per-tenant sequences.

    Enumerates tables, sequences, and column/sequence bindings from
    the source schema, then emits a batch of SQL that creates
    per-tenant copies of everything and repoints column defaults.

    REPLACES the old ``copy_table_structure``, which used
    ``CREATE TABLE ... LIKE ... INCLUDING ALL`` but did not copy
    sequences -- leaving defaults pointing at ``public.<seq>`` and
    making per-tenant stream ordering a fiction.
    """
    tables, sequences, sequence_defaults = discover_schema_surface(
        conn, source_schema
    )
    logger.info(
        "Cloning %d tables, %d sequences, %d sequence/column bindings "
        "from %s to %s",
        len(tables),
        len(sequences),
        len(sequence_defaults),
        source_schema,
        target_schema,
    )

    statements = build_clone_schema_sql(
        target_schema=target_schema,
        tables=tables,
        sequences=sequences,
        sequence_defaults=sequence_defaults,
        source_schema=source_schema,
    )

    with conn.cursor() as cur:
        for stmt in statements:
            try:
                cur.execute(stmt)
            except Exception as e:
                logger.error(
                    "Failed to execute bootstrap SQL: %s -- error: %s",
                    stmt,
                    e,
                )
                raise


def create_tenant_schema_from_template(
    db_params: dict,
    schema_name: str,
    template_schema: str = "public",
) -> None:
    """Create a new tenant schema from a template schema.

    Args:
        db_params: Database connection parameters.
        schema_name: The name of the schema to create.
        template_schema: The schema to copy structure from (default: public).
    """
    conn = psycopg2.connect(**db_params)
    conn.autocommit = True

    try:
        if schema_exists(conn, schema_name):
            logger.info(f"Schema {schema_name} already exists, skipping creation")
            return

        logger.info(f"Creating tenant schema: {schema_name}")
        create_schema(conn, schema_name)

        # Clone tables and sequences from the template
        conn.autocommit = False
        try:
            clone_schema_with_sequences(
                conn, template_schema, schema_name
            )
            conn.commit()
            logger.info(f"Successfully created tenant schema: {schema_name}")
        except Exception:
            conn.rollback()
            raise

    finally:
        conn.close()


def initialize_empty_schema(
    db_params: dict,
    schema_name: str,
) -> None:
    """Initialize an empty tenant schema.

    This creates just the schema without copying tables. Tables will be
    created when Synapse runs its normal schema setup on first start.

    Args:
        db_params: Database connection parameters.
        schema_name: The name of the schema to create.
    """
    conn = psycopg2.connect(**db_params)
    conn.autocommit = True

    try:
        if schema_exists(conn, schema_name):
            logger.info(f"Schema {schema_name} already exists")
            return

        logger.info(f"Creating empty tenant schema: {schema_name}")
        create_schema(conn, schema_name)
        logger.info(f"Successfully created empty schema: {schema_name}")

    finally:
        conn.close()


def get_tenants_from_config(config: dict) -> list[TenantConfig]:
    """Extract tenant configurations from Synapse config."""
    tenants = []

    tenants_list = config.get("tenants", [])
    for tenant_dict in tenants_list:
        try:
            tenant = TenantConfig.from_dict(tenant_dict)
            tenants.append(tenant)
        except Exception as e:
            logger.warning(f"Error parsing tenant config: {e}")

    return tenants


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create tenant schemas for multi-tenant Synapse"
    )
    parser.add_argument(
        "-c",
        "--config-path",
        required=True,
        help="Path to Synapse homeserver.yaml config file",
    )
    parser.add_argument(
        "-t",
        "--tenant-name",
        help="Server name of the tenant to create schema for",
    )
    parser.add_argument(
        "--all-tenants",
        action="store_true",
        help="Create schemas for all tenants defined in config",
    )
    parser.add_argument(
        "--schema-name",
        help="Override the schema name (only with --tenant-name)",
    )
    parser.add_argument(
        "--empty",
        action="store_true",
        help=(
            "Create an empty tenant schema without cloning tables or "
            "sequences. DANGEROUS under multi-tenant mode: tables will "
            "fall through to public. For debugging only."
        ),
    )
    parser.add_argument(
        "--template-schema",
        default="public",
        help="Schema to use as template (default: public)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose output",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.tenant_name and not args.all_tenants:
        parser.error("Either --tenant-name or --all-tenants must be specified")

    if args.tenant_name and args.all_tenants:
        parser.error("Cannot specify both --tenant-name and --all-tenants")

    if args.schema_name and args.all_tenants:
        parser.error("--schema-name cannot be used with --all-tenants")

    try:
        config = load_config(args.config_path)
    except Exception as e:
        logger.error(f"Failed to load config file: {e}")
        return 1

    try:
        db_params = get_db_connection_params(config)
    except Exception as e:
        logger.error(f"Failed to get database connection params: {e}")
        return 1

    # Test database connection
    try:
        conn = psycopg2.connect(**db_params)
        conn.close()
        logger.info("Database connection successful")
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        return 1

    if args.all_tenants:
        tenants = get_tenants_from_config(config)
        if not tenants:
            logger.error("No tenants found in configuration")
            return 1

        logger.info(f"Creating schemas for {len(tenants)} tenants")
        for tenant in tenants:
            try:
                if args.empty:
                    initialize_empty_schema(db_params, tenant.database_schema)
                else:
                    create_tenant_schema_from_template(
                        db_params,
                        tenant.database_schema,
                        args.template_schema,
                    )
            except Exception as e:
                logger.error(f"Failed to create schema for {tenant.server_name}: {e}")
                return 1

    else:
        # Single tenant
        tenants = get_tenants_from_config(config)
        tenant = next(
            (t for t in tenants if t.server_name == args.tenant_name), None
        )

        if tenant is None and args.schema_name is None:
            logger.error(
                f"Tenant {args.tenant_name} not found in config and no --schema-name provided"
            )
            return 1

        schema_name = args.schema_name or (tenant.database_schema if tenant else None)
        if schema_name is None:
            # Generate default schema name
            schema_name = "tenant_" + args.tenant_name.replace(".", "_").replace(
                "-", "_"
            )

        try:
            if args.empty:
                initialize_empty_schema(db_params, schema_name)
            else:
                create_tenant_schema_from_template(
                    db_params,
                    schema_name,
                    args.template_schema,
                )
        except Exception as e:
            logger.error(f"Failed to create schema {schema_name}: {e}")
            return 1

    logger.info("Schema creation completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
