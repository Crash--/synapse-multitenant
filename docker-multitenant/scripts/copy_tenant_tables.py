#!/usr/bin/env python3
"""
Copy table structure from public schema to tenant schemas.

This script should be run AFTER Synapse has created its tables in the public schema.
It copies the table definitions and initial singleton data to each tenant schema.
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


def wait_for_public_tables(conn, min_tables: int = 100, max_retries: int = 60) -> bool:
    """Wait for public schema to have tables (Synapse migration completed)."""
    cursor = conn.cursor()
    for i in range(max_retries):
        cursor.execute("""
            SELECT count(*) FROM pg_tables WHERE schemaname = 'public'
        """)
        count = cursor.fetchone()[0]
        if count >= min_tables:
            print(f"Public schema has {count} tables, proceeding with copy")
            cursor.close()
            return True
        print(f"Waiting for Synapse tables ({count}/{min_tables} tables)... ({i+1}/{max_retries})")
        time.sleep(5)
    cursor.close()
    return False


def get_tenant_schemas(conn) -> list:
    """Get list of tenant schemas."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'tenant_%'
        ORDER BY schema_name
    """)
    schemas = [row[0] for row in cursor.fetchall()]
    cursor.close()
    return schemas


def count_tables_in_schema(conn, schema: str) -> int:
    """Count tables in a schema."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT count(*) FROM pg_tables WHERE schemaname = %s
    """, (schema,))
    count = cursor.fetchone()[0]
    cursor.close()
    return count


def copy_table_structure(conn, source_schema: str, target_schema: str) -> int:
    """Copy table structure from source to target schema. Returns count of tables copied."""
    cursor = conn.cursor()

    # Get all tables in source schema
    cursor.execute("""
        SELECT tablename FROM pg_tables WHERE schemaname = %s ORDER BY tablename
    """, (source_schema,))
    tables = [row[0] for row in cursor.fetchall()]

    copied = 0
    for table in tables:
        # Check if table already exists in target schema
        cursor.execute("""
            SELECT 1 FROM pg_tables WHERE schemaname = %s AND tablename = %s
        """, (target_schema, table))
        if cursor.fetchone():
            continue  # Table already exists

        try:
            # Create table with same structure (INCLUDING ALL copies indexes, constraints, etc.)
            cursor.execute(f"""
                CREATE TABLE "{target_schema}"."{table}"
                (LIKE "{source_schema}"."{table}" INCLUDING ALL)
            """)
            conn.commit()
            copied += 1
        except Exception as e:
            conn.rollback()
            print(f"  Error copying table {table}: {e}")

    cursor.close()
    return copied


def copy_sequences(conn, source_schema: str, target_schema: str) -> int:
    """Copy sequences from source to target schema. Returns count of sequences copied."""
    cursor = conn.cursor()

    # Get all sequences in source schema
    cursor.execute("""
        SELECT sequencename FROM pg_sequences WHERE schemaname = %s ORDER BY sequencename
    """, (source_schema,))
    sequences = [row[0] for row in cursor.fetchall()]

    copied = 0
    for seq in sequences:
        # Check if sequence already exists
        cursor.execute("""
            SELECT 1 FROM pg_sequences WHERE schemaname = %s AND sequencename = %s
        """, (target_schema, seq))
        if cursor.fetchone():
            continue

        try:
            # Get sequence current value from source
            cursor.execute(f"""
                SELECT last_value, start_value, increment_by, max_value, min_value, cache_size, cycle
                FROM "{source_schema}"."{seq}"
            """)
            row = cursor.fetchone()
            if row:
                last_val, start_val, inc_by, max_val, min_val, cache_size, cycle = row

                # Create sequence with same parameters
                cycle_str = "CYCLE" if cycle else "NO CYCLE"
                cursor.execute(f"""
                    CREATE SEQUENCE "{target_schema}"."{seq}"
                    START WITH {start_val}
                    INCREMENT BY {inc_by}
                    MINVALUE {min_val}
                    MAXVALUE {max_val}
                    CACHE {cache_size}
                    {cycle_str}
                """)
                conn.commit()
                copied += 1
        except Exception as e:
            conn.rollback()
            print(f"  Error copying sequence {seq}: {e}")

    cursor.close()
    return copied


def copy_singleton_data(conn, target_schema: str) -> int:
    """Copy singleton/initialization data needed by Synapse."""
    cursor = conn.cursor()

    # Tables that need initial singleton rows for Synapse to function
    singleton_tables = [
        "room_forgetter_stream_pos",
        "un_partial_stated_room_stream",
        "device_lists_changes_converted_stream_position",
        "receipts_linearized_last_processed_stream_id",
        "local_current_membership",
        "user_directory_stream_pos",
        "current_state_delta_stream",
        "state_groups",
        "applied_schema_deltas",
        "schema_version",
        "delayed_events_stream_pos",
    ]

    copied = 0
    for table in singleton_tables:
        try:
            # Check if table exists in target
            cursor.execute("""
                SELECT 1 FROM pg_tables WHERE schemaname = %s AND tablename = %s
            """, (target_schema, table))
            if not cursor.fetchone():
                continue  # Table doesn't exist

            # Check if table has data in target
            cursor.execute(f"""
                SELECT count(*) FROM "{target_schema}"."{table}"
            """)
            target_count = cursor.fetchone()[0]
            if target_count > 0:
                continue  # Already has data

            # Check if source has data
            cursor.execute(f"""
                SELECT count(*) FROM "public"."{table}"
            """)
            source_count = cursor.fetchone()[0]
            if source_count == 0:
                continue  # No source data to copy

            # Copy data from public to target
            cursor.execute(f"""
                INSERT INTO "{target_schema}"."{table}"
                SELECT * FROM "public"."{table}"
            """)
            conn.commit()
            copied += 1
            print(f"  Copied singleton data: {table} ({source_count} rows)")
        except Exception as e:
            conn.rollback()
            # Some tables may have constraints that prevent copying, that's OK
            pass

    cursor.close()
    return copied


def main():
    """Main entry point."""
    # Database connection parameters
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    user = os.environ.get("POSTGRES_USER", "synapse")
    password = os.environ.get("POSTGRES_PASSWORD", "synapse_password")
    db = os.environ.get("POSTGRES_DB", "synapse_multitenant")

    print(f"Connecting to PostgreSQL at {host}:{port}/{db}")

    try:
        conn = psycopg2.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=db,
        )
        conn.autocommit = False
    except Exception as e:
        print(f"Failed to connect: {e}", file=sys.stderr)
        sys.exit(1)

    # Wait for Synapse to create tables in public schema
    if not wait_for_public_tables(conn):
        print("Timeout waiting for public schema tables", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # Get tenant schemas
    tenant_schemas = get_tenant_schemas(conn)
    if not tenant_schemas:
        print("No tenant schemas found (expecting tenant_*)")
        conn.close()
        sys.exit(0)

    print(f"Found {len(tenant_schemas)} tenant schemas: {', '.join(tenant_schemas)}")

    # Copy table structure to each tenant schema
    for schema in tenant_schemas:
        existing = count_tables_in_schema(conn, schema)
        print(f"\nProcessing {schema} (currently has {existing} tables)...")

        # Copy tables
        tables_copied = copy_table_structure(conn, "public", schema)
        print(f"  Copied {tables_copied} table structures")

        # Copy sequences
        seqs_copied = copy_sequences(conn, "public", schema)
        print(f"  Copied {seqs_copied} sequences")

        # Copy singleton data
        singletons_copied = copy_singleton_data(conn, schema)
        print(f"  Initialized {singletons_copied} singleton tables")

        # Final count
        final_count = count_tables_in_schema(conn, schema)
        print(f"  {schema} now has {final_count} tables")

    conn.close()
    print("\nDone! Tenant schemas are ready for use.")


if __name__ == "__main__":
    main()
