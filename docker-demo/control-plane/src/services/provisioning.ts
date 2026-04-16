import type { Pool } from "pg";

/**
 * Provision a tenant database schema by cloning from the public schema.
 *
 * This is a TypeScript port of the schema cloning logic from
 * scripts/create_tenant_schema.py and docker-demo/manager/app/lib/provision.ts.
 *
 * Steps:
 * 1. Create the schema
 * 2. Clone all tables from public (INCLUDING ALL)
 * 3. Clone sequences with current state
 * 4. Copy singleton seed rows
 */
export async function provisionTenantSchema(
  pool: Pool,
  schemaName: string
): Promise<void> {
  // Validate schema name to prevent SQL injection
  if (!/^[a-zA-Z0-9_]+$/.test(schemaName)) {
    throw new Error(`Invalid schema name: ${schemaName}`);
  }

  // Step 1: Create schema
  const exists = await pool.query(
    "SELECT schema_name FROM information_schema.schemata WHERE schema_name = $1",
    [schemaName]
  );
  if (exists.rows.length > 0) {
    throw new Error(`Schema '${schemaName}' already exists`);
  }
  await pool.query(`CREATE SCHEMA "${schemaName}"`);

  // Step 2: Clone tables from public
  const tablesResult = await pool.query(
    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
  );

  for (const row of tablesResult.rows) {
    const table: string = row.tablename;
    // Skip the tenants management table itself
    if (table === "tenants") continue;

    try {
      await pool.query(
        `CREATE TABLE "${schemaName}"."${table}" (LIKE "public"."${table}" INCLUDING ALL)`
      );
    } catch {
      // Some tables may fail due to dependencies, continue
    }
  }

  // Step 3: Clone sequences with current state
  const seqsResult = await pool.query(
    `SELECT sequencename, start_value, increment_by, max_value, min_value,
            cache_size, cycle, last_value
     FROM pg_sequences WHERE schemaname = 'public' ORDER BY sequencename`
  );

  for (const s of seqsResult.rows) {
    try {
      const cycleStr = s.cycle ? "CYCLE" : "NO CYCLE";
      await pool.query(
        `CREATE SEQUENCE "${schemaName}"."${s.sequencename}"
         START WITH ${s.start_value} INCREMENT BY ${s.increment_by}
         MINVALUE ${s.min_value} MAXVALUE ${s.max_value}
         CACHE ${s.cache_size} ${cycleStr}`
      );
      // Advance to current value if it's been used
      if (s.last_value != null && s.last_value !== s.start_value) {
        await pool.query(
          `SELECT setval('"${schemaName}"."${s.sequencename}"', $1, true)`,
          [s.last_value]
        );
      }
    } catch {
      // Continue on error (sequence may already exist from INCLUDING ALL)
    }
  }

  // Step 4: Copy singleton seed rows.
  // This list must stay in sync with scripts/create_tenant_schema.py::
  // SINGLETON_SEED_TABLES. Drift here causes bg-process crashes and
  // register/send failures (critical-e2e-bugs.md B-1).
  const singletonTables = [
    // --- Canonical list from SINGLETON_SEED_TABLES (Python bootstrap) ---
    "appservice_stream_position",
    "event_push_summary_last_receipt_stream_id",
    "event_push_summary_stream_ordering",
    "federation_stream_position",
    "stats_incremental_position",
    "user_directory_stream_pos",
    "room_forgetter_stream_pos",
    "delayed_events_stream_pos",
    "device_lists_changes_converted_stream_position",
    // --- Additional tables the control-plane has historically seeded;
    //     kept for safety. Harmless if empty in public. ---
    "schema_version",
    "applied_schema_deltas",
    "un_partial_stated_room_stream",
    "receipts_linearized_last_processed_stream_id",
    "current_state_delta_stream",
  ];

  for (const table of singletonTables) {
    try {
      const targetCount = await pool.query(
        `SELECT count(*) FROM "${schemaName}"."${table}"`
      );
      if (parseInt(targetCount.rows[0].count) > 0) continue;

      const sourceCount = await pool.query(
        `SELECT count(*) FROM "public"."${table}"`
      );
      if (parseInt(sourceCount.rows[0].count) === 0) continue;

      await pool.query(
        `INSERT INTO "${schemaName}"."${table}" SELECT * FROM "public"."${table}" ON CONFLICT DO NOTHING`
      );
    } catch {
      // Table may not exist in target yet
    }
  }
}
