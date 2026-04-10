import { z } from "zod";

const configSchema = z.object({
  PORT: z.coerce.number().default(3001),
  DATABASE_URL: z.string(),

  // Shared secret for authenticating API requests
  API_TOKEN: z.string(),

  // AES-256-GCM master key for encrypting tenant signing keys (base64-encoded 32 bytes)
  TENANT_KEY_MASTER: z.string(),

  // Synapse URL for pushing reload notifications
  SYNAPSE_URL: z.string().default("http://synapse:8008"),

  // Shared secret matching Synapse's multi_tenant.reload_secret
  SYNAPSE_RELOAD_SECRET: z.string(),

  // Base path for tenant media directories
  MEDIA_BASE_DIR: z.string().default("/media"),
});

export type Config = z.infer<typeof configSchema>;

export function loadConfig(): Config {
  return configSchema.parse(process.env);
}
