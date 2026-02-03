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


def copy_table_structure(conn, source_schema: str, target_schema: str) -> None:
    """Copy table structure from source schema to target schema.

    This copies the table definitions, indexes, and constraints from the
    public schema (or another source) to the tenant schema.
    """
    with conn.cursor() as cur:
        # Get all tables from source schema
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

        logger.info(
            f"Copying {len(tables)} tables from {source_schema} to {target_schema}"
        )

        for table in tables:
            # Get the CREATE TABLE statement
            # We use pg_dump style recreation
            cur.execute(
                f"""
                SELECT 'CREATE TABLE {target_schema}.' || quote_ident(c.relname) || ' (LIKE {source_schema}.' || quote_ident(c.relname) || ' INCLUDING ALL)'
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'r'
                """,
                (source_schema, table),
            )
            result = cur.fetchone()
            if result:
                try:
                    cur.execute(result[0])
                    logger.debug(f"  Created table: {target_schema}.{table}")
                except psycopg2.errors.DuplicateTable:
                    logger.debug(f"  Table already exists: {target_schema}.{table}")
                except Exception as e:
                    logger.warning(f"  Error creating table {table}: {e}")


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

        # Copy table structure from template
        conn.autocommit = False
        try:
            copy_table_structure(conn, template_schema, schema_name)
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
        "--from-template",
        action="store_true",
        help="Copy table structure from public schema (for migration)",
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
                if args.from_template:
                    create_tenant_schema_from_template(
                        db_params,
                        tenant.database_schema,
                        args.template_schema,
                    )
                else:
                    initialize_empty_schema(db_params, tenant.database_schema)
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
            if args.from_template:
                create_tenant_schema_from_template(
                    db_params,
                    schema_name,
                    args.template_schema,
                )
            else:
                initialize_empty_schema(db_params, schema_name)
        except Exception as e:
            logger.error(f"Failed to create schema {schema_name}: {e}")
            return 1

    logger.info("Schema creation completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
