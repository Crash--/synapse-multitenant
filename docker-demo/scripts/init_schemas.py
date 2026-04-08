#!/usr/bin/env python3
"""Create one PostgreSQL schema per demo tenant.

Schema name = ``tenant_`` + server_name with dots and dashes replaced by
underscores, matching the ``database_schema`` values in the demo's
homeserver.yaml.
"""

import os
import sys
import time

try:
    import psycopg2
except ImportError:
    import subprocess
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", "psycopg2-binary"]
    )
    import psycopg2


def wait_for_postgres(host, port, user, password, db, max_retries=30):
    for i in range(max_retries):
        try:
            conn = psycopg2.connect(
                host=host, port=port, user=user, password=password, database=db,
            )
            conn.close()
            print(f"PostgreSQL is ready at {host}:{port}")
            return
        except psycopg2.OperationalError:
            print(f"Waiting for PostgreSQL ({i+1}/{max_retries})…")
            time.sleep(2)
    print("Failed to connect to PostgreSQL", file=sys.stderr)
    sys.exit(1)


def create_schema(conn, schema_name):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name = %s",
        (schema_name,),
    )
    if cursor.fetchone():
        print(f"  [SKIP] {schema_name} — already exists")
        cursor.close()
        return
    cursor.execute(f'CREATE SCHEMA "{schema_name}"')
    conn.commit()
    print(f"  [OK]   {schema_name}")
    cursor.close()


def main():
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    user = os.environ.get("POSTGRES_USER", "synapse")
    password = os.environ.get("POSTGRES_PASSWORD", "synapse_password")
    db = os.environ.get("POSTGRES_DB", "synapse_demo")
    tenants = [
        t.strip()
        for t in os.environ.get(
            "TENANTS", "matrix.tenant-a.com,matrix.tenant-b.com"
        ).split(",")
        if t.strip()
    ]

    wait_for_postgres(host, port, user, password, db)

    conn = psycopg2.connect(
        host=host, port=port, user=user, password=password, database=db,
    )
    conn.autocommit = True

    print(f"Creating schemas for: {', '.join(tenants)}")
    for tenant in tenants:
        schema_name = "tenant_" + tenant.replace(".", "_").replace("-", "_")
        try:
            create_schema(conn, schema_name)
        except Exception as e:
            print(f"Error creating schema for {tenant}: {e}", file=sys.stderr)
            conn.close()
            sys.exit(1)

    conn.close()
    print("All schemas initialized.")


if __name__ == "__main__":
    main()
