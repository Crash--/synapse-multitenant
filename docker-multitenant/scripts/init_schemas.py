#!/usr/bin/env python3
"""
Initialize PostgreSQL schemas for multi-tenant Synapse.

This script creates the database schemas for each configured tenant
and ensures proper permissions.
"""

import os
import sys
import time

try:
    import psycopg2
except ImportError:
    print("Installing psycopg2-binary...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "psycopg2-binary", "-q"])
    import psycopg2


def wait_for_postgres(host: str, port: int, user: str, password: str, db: str, max_retries: int = 30) -> None:
    """Wait for PostgreSQL to be ready."""
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
        except psycopg2.OperationalError as e:
            print(f"Waiting for PostgreSQL ({i+1}/{max_retries})...")
            time.sleep(2)

    print("Failed to connect to PostgreSQL", file=sys.stderr)
    sys.exit(1)


def create_schema(conn, schema_name: str) -> None:
    """Create a schema if it doesn't exist."""
    cursor = conn.cursor()

    # Check if schema exists
    cursor.execute(
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name = %s",
        (schema_name,)
    )
    if cursor.fetchone():
        print(f"Schema '{schema_name}' already exists")
        cursor.close()
        return

    # Create schema
    cursor.execute(f'CREATE SCHEMA "{schema_name}"')
    conn.commit()
    print(f"Created schema '{schema_name}'")
    cursor.close()


def main():
    """Initialize schemas for all tenants."""
    # Database connection parameters
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    user = os.environ.get("POSTGRES_USER", "synapse")
    password = os.environ.get("POSTGRES_PASSWORD", "synapse")
    db = os.environ.get("POSTGRES_DB", "synapse")

    # Tenant configurations
    tenants = os.environ.get("TENANTS", "acme.localhost,corp.localhost,startup.localhost").split(",")

    # Wait for PostgreSQL to be ready
    wait_for_postgres(host, port, user, password, db)

    # Connect to database
    conn = psycopg2.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=db,
    )
    conn.autocommit = True

    # Create schema for each tenant
    for tenant in tenants:
        tenant = tenant.strip()
        if not tenant:
            continue

        # Schema name is tenant_<sanitized_name>
        schema_name = "tenant_" + tenant.replace(".", "_").replace("-", "_")
        try:
            create_schema(conn, schema_name)
        except Exception as e:
            print(f"Error creating schema for {tenant}: {e}", file=sys.stderr)
            conn.close()
            sys.exit(1)

    conn.close()
    print("All schemas initialized successfully")


if __name__ == "__main__":
    main()
