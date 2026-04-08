#!/usr/bin/env python3
"""
Initialize PostgreSQL tenant schemas for multi-tenant Synapse.

This script is the docker-multitenant bootstrap for per-tenant schemas.
It MUST run AFTER Synapse has populated the ``public`` schema with its
migrations (i.e. after the ``synapse`` service is healthy) and BEFORE any
probes or user traffic hit the tenant endpoints.

It reads the authoritative list of tenant schemas from homeserver.yaml
(``multi_tenant.tenants[*].database_schema``), waits for ``public`` to be
populated, then clones the public schema into each tenant schema using
``scripts.create_tenant_schema.clone_schema_with_sequences`` so each
tenant gets its OWN tables, sequences, and per-tenant column defaults.

Previously this script only ``CREATE SCHEMA``-ed empty namespaces and
relied on ``search_path`` fallthrough to ``public``. That leaked writes
across tenants (see Phase 1B isolation probes). Task 4 of the phase 1
close plan replaces that with a real per-tenant clone.
"""

from __future__ import annotations

import os
import sys
import time
from typing import List

try:
    import psycopg2
except ImportError:
    print("Installing psycopg2-binary...")
    import subprocess

    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "psycopg2-binary", "-q"]
    )
    import psycopg2

try:
    import yaml
except ImportError:
    print("Installing pyyaml...")
    import subprocess

    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyyaml", "-q"])
    import yaml

# Make ``scripts.create_tenant_schema`` importable. This script runs inside
# the synapse-multitenant image where the repo is copied to /src (see
# Dockerfile.real). Fall back to sys.path search if that's not the case.
for candidate in ("/src", "/synapse", "/app"):
    if os.path.isdir(os.path.join(candidate, "scripts")):
        sys.path.insert(0, candidate)
        break

try:
    from scripts.create_tenant_schema import (  # type: ignore
        clone_schema_with_sequences,
        create_schema,
        schema_exists,
    )
except ImportError as e:
    print(
        f"ERROR: Could not import scripts.create_tenant_schema: {e}\n"
        "Make sure the repo is mounted at /src in the init-schemas container.",
        file=sys.stderr,
    )
    sys.exit(1)


CONFIG_PATH = os.environ.get("SYNAPSE_CONFIG_PATH", "/data/homeserver.yaml")
MIN_PUBLIC_TABLES = int(os.environ.get("MIN_PUBLIC_TABLES", "150"))
WAIT_MAX_RETRIES = int(os.environ.get("WAIT_MAX_RETRIES", "120"))
WAIT_INTERVAL_SECS = int(os.environ.get("WAIT_INTERVAL_SECS", "2"))


def wait_for_postgres(
    host: str, port: int, user: str, password: str, db: str, max_retries: int = 60
) -> None:
    """Wait for PostgreSQL to accept connections."""
    for i in range(max_retries):
        try:
            conn = psycopg2.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=db,
            )
            conn.close()
            print(f"PostgreSQL is ready at {host}:{port}")
            return
        except psycopg2.OperationalError:
            print(f"Waiting for PostgreSQL ({i + 1}/{max_retries})...")
            time.sleep(2)

    print("Failed to connect to PostgreSQL", file=sys.stderr)
    sys.exit(1)


def wait_for_public_schema(conn, min_tables: int) -> None:
    """Block until ``public`` has at least ``min_tables`` BASE TABLEs.

    Synapse populates ``public`` during startup via prepare_database. The
    bootstrap cannot clone an empty source, so we poll until the table
    count stabilises above a floor.
    """
    with conn.cursor() as cur:
        for i in range(WAIT_MAX_RETRIES):
            cur.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
            count = cur.fetchone()[0]
            if count >= min_tables:
                print(
                    f"public schema ready: {count} tables (>= {min_tables})"
                )
                return
            print(
                f"Waiting for public schema ({count}/{min_tables} tables) "
                f"[{i + 1}/{WAIT_MAX_RETRIES}]..."
            )
            time.sleep(WAIT_INTERVAL_SECS)
    print(
        f"Timed out waiting for public schema to reach {min_tables} tables",
        file=sys.stderr,
    )
    sys.exit(1)


def load_tenant_schemas(config_path: str) -> List[str]:
    """Read authoritative tenant schema names from homeserver.yaml."""
    if not os.path.exists(config_path):
        print(
            f"ERROR: homeserver config not found at {config_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f) or {}

    # Authoritative source: top-level ``tenants:`` block. Some configs
    # also nest it under ``multi_tenant.tenants``; accept both.
    tenants = cfg.get("tenants") or (cfg.get("multi_tenant") or {}).get(
        "tenants"
    ) or []
    if not tenants:
        print(
            "ERROR: no tenants[] found in homeserver.yaml (checked top-level "
            "and multi_tenant.tenants)",
            file=sys.stderr,
        )
        sys.exit(1)

    schemas: List[str] = []
    for t in tenants:
        schema = t.get("database_schema")
        if not schema:
            print(
                f"WARNING: tenant missing database_schema: {t}", file=sys.stderr
            )
            continue
        schemas.append(schema)

    if not schemas:
        print("ERROR: no tenant schemas resolved from config", file=sys.stderr)
        sys.exit(1)

    return schemas


def schema_has_tables(conn, schema: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
            (schema,),
        )
        return cur.fetchone()[0]


def bootstrap_tenant(conn, schema: str) -> None:
    """Create ``schema`` (if missing) and clone public into it.

    Idempotent: if the schema already has Synapse tables, leaves it alone.
    """
    if not schema_exists(conn, schema):
        conn.autocommit = True
        create_schema(conn, schema)
        conn.autocommit = False

    existing = schema_has_tables(conn, schema)
    if existing >= MIN_PUBLIC_TABLES:
        print(
            f"Schema '{schema}' already has {existing} tables, skipping clone"
        )
        return

    if existing > 0:
        print(
            f"WARNING: schema '{schema}' has {existing} tables (less than "
            f"{MIN_PUBLIC_TABLES}); cloning anyway into existing namespace"
        )

    print(f"Cloning public -> {schema} ...")
    try:
        clone_schema_with_sequences(conn, "public", schema)
        conn.commit()
        final = schema_has_tables(conn, schema)
        print(f"Schema '{schema}' now has {final} tables")
    except Exception as e:
        conn.rollback()
        print(f"ERROR cloning into {schema}: {e}", file=sys.stderr)
        raise


def main() -> None:
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    user = os.environ.get("POSTGRES_USER", "synapse")
    password = os.environ.get("POSTGRES_PASSWORD", "synapse")
    db = os.environ.get("POSTGRES_DB", "synapse")

    wait_for_postgres(host, port, user, password, db)

    schemas = load_tenant_schemas(CONFIG_PATH)
    print(f"Tenant schemas from {CONFIG_PATH}: {schemas}")

    conn = psycopg2.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=db,
    )
    conn.autocommit = False

    try:
        wait_for_public_schema(conn, MIN_PUBLIC_TABLES)

        for schema in schemas:
            bootstrap_tenant(conn, schema)
    finally:
        conn.close()

    print("All tenant schemas initialised successfully")


if __name__ == "__main__":
    main()
