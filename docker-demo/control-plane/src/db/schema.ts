import {
  boolean,
  integer,
  jsonb,
  pgTable,
  text,
  timestamp,
  uuid,
  customType,
} from "drizzle-orm/pg-core";

// Custom type for BYTEA columns
const bytea = customType<{ data: Buffer; driverData: Buffer }>({
  dataType() {
    return "bytea";
  },
});

export const tenants = pgTable("tenants", {
  id: uuid("id").primaryKey().defaultRandom(),
  serverName: text("server_name").notNull().unique(),
  databaseSchema: text("database_schema").notNull().unique(),
  status: text("status")
    .notNull()
    .default("provisioning")
    .$type<"provisioning" | "active" | "suspended" | "deleting">(),

  // Signing key (AES-256-GCM encrypted)
  signingKeyEncrypted: bytea("signing_key_encrypted").notNull(),
  signingKeyId: text("signing_key_id").notNull(),

  // Secrets
  macaroonSecretKey: text("macaroon_secret_key"),
  formSecret: text("form_secret"),
  registrationSharedSecret: text("registration_shared_secret"),

  // Core config
  mediaStorePath: text("media_store_path").notNull(),
  registrationEnabled: boolean("registration_enabled").notNull().default(false),
  enableFederation: boolean("enable_federation").notNull().default(true),
  maxMauValue: integer("max_mau_value").notNull().default(0),
  publicBaseurl: text("public_baseurl"),
  serverNoticesMxid: text("server_notices_mxid"),
  trustedKeyServers: jsonb("trusted_key_servers").default([]),

  // Complex sub-configs (JSONB)
  emailConfig: jsonb("email_config"),
  oidcConfig: jsonb("oidc_config"),
  casConfig: jsonb("cas_config"),
  samlConfig: jsonb("saml_config"),
  pushConfig: jsonb("push_config"),
  ratelimitConfig: jsonb("ratelimit_config"),
  appServiceConfigFiles: jsonb("app_service_config_files"),

  // Audit
  createdAt: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  configVersion: integer("config_version").notNull().default(1),
});

export type Tenant = typeof tenants.$inferSelect;
export type NewTenant = typeof tenants.$inferInsert;
