import { drizzle } from "drizzle-orm/node-postgres";
import pg from "pg";
import * as schema from "./schema.js";

let pool: pg.Pool | null = null;

export function getPool(databaseUrl: string): pg.Pool {
  if (!pool) {
    pool = new pg.Pool({ connectionString: databaseUrl });
  }
  return pool;
}

export function getDb(databaseUrl: string) {
  return drizzle(getPool(databaseUrl), { schema });
}

export type Database = ReturnType<typeof getDb>;

/**
 * Create the public.tenants table if it doesn't exist.
 * Uses raw SQL so we don't need drizzle-kit at runtime.
 */
export async function ensureSchema(pool: pg.Pool): Promise<void> {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS public.tenants (
      id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      server_name     TEXT NOT NULL UNIQUE,
      database_schema TEXT NOT NULL UNIQUE,
      status          TEXT NOT NULL DEFAULT 'provisioning'
                      CHECK (status IN ('provisioning','active','suspended','deleting')),

      signing_key_encrypted       BYTEA NOT NULL,
      signing_key_id              TEXT  NOT NULL,

      macaroon_secret_key         TEXT,
      form_secret                 TEXT,
      registration_shared_secret  TEXT,

      media_store_path        TEXT    NOT NULL,
      registration_enabled    BOOLEAN NOT NULL DEFAULT false,
      enable_federation       BOOLEAN NOT NULL DEFAULT true,
      max_mau_value           INTEGER NOT NULL DEFAULT 0,
      public_baseurl          TEXT,
      server_notices_mxid     TEXT,
      trusted_key_servers     JSONB   DEFAULT '[]',

      email_config            JSONB,
      oidc_config             JSONB,
      cas_config              JSONB,
      saml_config             JSONB,
      push_config             JSONB,
      ratelimit_config        JSONB,
      app_service_config_files JSONB,

      created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
      config_version   INTEGER     NOT NULL DEFAULT 1
    );
  `);
}
