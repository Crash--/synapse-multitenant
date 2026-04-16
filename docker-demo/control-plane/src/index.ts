import Fastify from "fastify";
import { loadConfig } from "./config.js";
import { getDb, getPool, ensureSchema } from "./db/connection.js";
import { createAuthHook } from "./middleware/auth.js";
import { tenantRoutes } from "./routes/tenants.js";
import { federationTestRoutes } from "./routes/federation-test.js";

async function main() {
  const config = loadConfig();
  const db = getDb(config.DATABASE_URL);
  const pool = getPool(config.DATABASE_URL);

  // Ensure the tenants table exists
  await ensureSchema(pool);

  const app = Fastify({ logger: true });

  // Health check (unauthenticated)
  app.get("/api/health", async () => ({ status: "ok" }));

  // All other routes require bearer token
  app.addHook("preHandler", async (request, reply) => {
    // Skip auth for health check
    if (request.url === "/api/health") return;
    return createAuthHook(config.API_TOKEN)(request, reply);
  });

  // Register tenant routes
  await app.register(tenantRoutes, { db, config, pool });
  await app.register(federationTestRoutes, { config });

  // Start server
  await app.listen({ port: config.PORT, host: "0.0.0.0" });
  console.log(`Control plane listening on port ${config.PORT}`);
}

main().catch((err) => {
  console.error("Failed to start control plane:", err);
  process.exit(1);
});
