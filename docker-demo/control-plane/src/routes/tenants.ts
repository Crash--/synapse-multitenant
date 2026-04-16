import type { FastifyInstance } from "fastify";
import { eq } from "drizzle-orm";
import { randomBytes } from "node:crypto";
import { tenants } from "../db/schema.js";
import type { Database } from "../db/connection.js";
import type { Config } from "../config.js";
import {
  encryptSigningKey,
  generateSigningKey,
  parseMasterKey,
} from "../services/key-manager.js";
import { reloadSynapseTenants } from "../services/synapse-client.js";
import { provisionTenantSchema } from "../services/provisioning.js";

const RESERVED_NAMES = new Set([
  "localhost",
  "manager.localhost",
  "control-plane.localhost",
  "traefik.localhost",
]);

type ProvisionArgs = {
  db: any;
  config: any;
  pool: any;
  masterKey: Buffer;
  serverName: string;
  body: any;
  emit: (event: string, data: unknown) => void;
};

async function provisionTenant(args: ProvisionArgs): Promise<any> {
  const { db, config, pool, masterKey, serverName, body, emit } = args;
  const safeName = serverName.replace(/[^a-zA-Z0-9]/g, "_");
  const databaseSchema = `tenant_${safeName}`;
  const mediaStorePath = `${config.MEDIA_BASE_DIR}/${serverName}`;

  emit("step", { step: "keygen", status: "running" });
  const { keyText, keyId } = generateSigningKey(serverName);
  const signingKeyEncrypted = encryptSigningKey(keyText, masterKey);
  emit("step", { step: "keygen", status: "ok", detail: { keyId } });

  const macaroonSecretKey = randomBytes(32).toString("hex");
  const formSecret = randomBytes(32).toString("hex");

  emit("step", { step: "db_row", status: "running" });
  const [inserted] = await db
    .insert(tenants)
    .values({
      serverName,
      databaseSchema,
      status: "provisioning",
      signingKeyEncrypted,
      signingKeyId: keyId,
      macaroonSecretKey,
      formSecret,
      mediaStorePath,
      registrationEnabled: body.registration_enabled ?? false,
      enableFederation: body.enable_federation ?? true,
      maxMauValue: body.max_mau_value ?? 0,
      publicBaseurl: body.public_baseurl ?? `https://${serverName}/`,
    })
    .returning();
  emit("step", { step: "db_row", status: "ok" });

  emit("step", { step: "schema_clone", status: "running" });
  try {
    await provisionTenantSchema(pool, databaseSchema);
    emit("step", { step: "schema_clone", status: "ok" });
  } catch (err: any) {
    emit("step", { step: "schema_clone", status: "error", detail: err.message });
    throw err;
  }

  emit("step", { step: "media_dir", status: "running" });
  try {
    const { mkdir } = await import("node:fs/promises");
    await mkdir(mediaStorePath, { recursive: true });
    emit("step", { step: "media_dir", status: "ok" });
  } catch (err: any) {
    emit("step", { step: "media_dir", status: "ok", detail: "non-fatal: " + err.message });
  }

  emit("step", { step: "activate", status: "running" });
  await db
    .update(tenants)
    .set({ status: "active", updatedAt: new Date() })
    .where(eq(tenants.id, inserted.id));
  emit("step", { step: "activate", status: "ok" });

  emit("step", { step: "synapse_reload", status: "running" });
  try {
    await reloadSynapseTenants(config.SYNAPSE_URL, config.SYNAPSE_RELOAD_SECRET);
    emit("step", { step: "synapse_reload", status: "ok" });
  } catch (err: any) {
    emit("step", { step: "synapse_reload", status: "ok", detail: "non-fatal: " + err.message });
  }

  return {
    id: inserted.id,
    server_name: serverName,
    database_schema: databaseSchema,
    status: "active",
  };
}

export async function tenantRoutes(
  app: FastifyInstance,
  opts: { db: Database; config: Config; pool: import("pg").Pool }
) {
  const { db, config, pool } = opts;
  const masterKey = parseMasterKey(config.TENANT_KEY_MASTER);

  // GET /api/v1/tenants — list all tenants
  app.get("/api/v1/tenants", async () => {
    const rows = await db.select().from(tenants);
    return {
      tenants: rows.map((t) => ({
        id: t.id,
        server_name: t.serverName,
        database_schema: t.databaseSchema,
        status: t.status,
        registration_enabled: t.registrationEnabled,
        enable_federation: t.enableFederation,
        max_mau_value: t.maxMauValue,
        created_at: t.createdAt,
      })),
      total: rows.length,
    };
  });

  // GET /api/v1/tenants/:serverName — get tenant details
  app.get<{ Params: { serverName: string } }>(
    "/api/v1/tenants/:serverName",
    async (request, reply) => {
      const { serverName } = request.params;
      const [tenant] = await db
        .select()
        .from(tenants)
        .where(eq(tenants.serverName, serverName));

      if (!tenant) {
        return reply.status(404).send({ error: `Tenant '${serverName}' not found` });
      }

      return {
        id: tenant.id,
        server_name: tenant.serverName,
        database_schema: tenant.databaseSchema,
        status: tenant.status,
        media_store_path: tenant.mediaStorePath,
        registration_enabled: tenant.registrationEnabled,
        enable_federation: tenant.enableFederation,
        max_mau_value: tenant.maxMauValue,
        public_baseurl: tenant.publicBaseurl,
        email_config: tenant.emailConfig,
        oidc_config: tenant.oidcConfig,
        push_config: tenant.pushConfig,
        ratelimit_config: tenant.ratelimitConfig,
        created_at: tenant.createdAt,
        updated_at: tenant.updatedAt,
        config_version: tenant.configVersion,
      };
    }
  );

  // POST /api/v1/tenants — create a new tenant (full provisioning)
  app.post<{
    Body: {
      server_name: string;
      registration_enabled?: boolean;
      enable_federation?: boolean;
      max_mau_value?: number;
      public_baseurl?: string;
    };
  }>("/api/v1/tenants", async (request, reply) => {
    const body = request.body as any;
    const serverName = body.server_name;

    if (!serverName || typeof serverName !== "string") {
      return reply.status(400).send({ error: "Missing required field: server_name" });
    }

    if (RESERVED_NAMES.has(serverName)) {
      return reply.status(400).send({ error: `'${serverName}' is a reserved name; pick another` });
    }

    const [existing] = await db
      .select()
      .from(tenants)
      .where(eq(tenants.serverName, serverName));
    if (existing) {
      return reply.status(409).send({ error: `Tenant '${serverName}' already exists` });
    }

    const wantsSSE = (request.headers.accept ?? "").includes("text/event-stream");

    if (wantsSSE) {
      reply.raw.setHeader("Content-Type", "text/event-stream");
      reply.raw.setHeader("Cache-Control", "no-cache");
      reply.raw.setHeader("Connection", "keep-alive");
      reply.raw.flushHeaders();

      const emit = (event: string, data: unknown) => {
        reply.raw.write(`event: ${event}\n`);
        reply.raw.write(`data: ${JSON.stringify(data)}\n\n`);
      };

      try {
        const result = await provisionTenant({ db, config, pool, masterKey, serverName, body, emit });
        emit("complete", { tenant: result });
      } catch (err: any) {
        emit("error", { message: err.message });
      } finally {
        reply.raw.end();
      }
      return reply;
    }

    // JSON path
    try {
      const result = await provisionTenant({
        db, config, pool, masterKey, serverName, body,
        emit: () => {},
      });
      return reply.status(201).send({ tenant: result, steps: [] });
    } catch (err: any) {
      return reply.status(500).send({ error: err.message });
    }
  });

  // PATCH /api/v1/tenants/:serverName — update tenant config
  app.patch<{ Params: { serverName: string } }>(
    "/api/v1/tenants/:serverName",
    async (request, reply) => {
      const { serverName } = request.params;
      const body = request.body as Record<string, any>;

      const [tenant] = await db
        .select()
        .from(tenants)
        .where(eq(tenants.serverName, serverName));

      if (!tenant) {
        return reply.status(404).send({ error: `Tenant '${serverName}' not found` });
      }

      // Build update object from allowed fields
      const updates: Record<string, any> = {};
      const allowedFields: Record<string, keyof typeof tenants> = {
        registration_enabled: "registrationEnabled" as any,
        enable_federation: "enableFederation" as any,
        max_mau_value: "maxMauValue" as any,
        public_baseurl: "publicBaseurl" as any,
        server_notices_mxid: "serverNoticesMxid" as any,
        email_config: "emailConfig" as any,
        oidc_config: "oidcConfig" as any,
        push_config: "pushConfig" as any,
        ratelimit_config: "ratelimitConfig" as any,
      };

      for (const [apiField, dbField] of Object.entries(allowedFields)) {
        if (apiField in body) {
          updates[dbField] = body[apiField];
        }
      }

      if (Object.keys(updates).length === 0) {
        return reply
          .status(400)
          .send({ error: "No updatable fields provided" });
      }

      updates.updatedAt = new Date();
      updates.configVersion = tenant.configVersion + 1;

      await db
        .update(tenants)
        .set(updates)
        .where(eq(tenants.serverName, serverName));

      // Push reload to Synapse
      try {
        await reloadSynapseTenants(config.SYNAPSE_URL, config.SYNAPSE_RELOAD_SECRET);
      } catch {
        // Non-fatal
      }

      return { server_name: serverName, updated_fields: Object.keys(updates) };
    }
  );

  // DELETE /api/v1/tenants/:serverName — soft delete
  app.delete<{ Params: { serverName: string } }>(
    "/api/v1/tenants/:serverName",
    async (request, reply) => {
      const { serverName } = request.params;

      const [tenant] = await db
        .select()
        .from(tenants)
        .where(eq(tenants.serverName, serverName));

      if (!tenant) {
        return reply.status(404).send({ error: `Tenant '${serverName}' not found` });
      }

      await db
        .update(tenants)
        .set({ status: "deleting", updatedAt: new Date() })
        .where(eq(tenants.serverName, serverName));

      // Push reload so Synapse removes the tenant
      try {
        await reloadSynapseTenants(config.SYNAPSE_URL, config.SYNAPSE_RELOAD_SECRET);
      } catch {
        // Non-fatal
      }

      return {
        server_name: serverName,
        status: "deleting",
      };
    }
  );

  // POST /api/v1/tenants/:serverName/suspend
  app.post<{ Params: { serverName: string } }>(
    "/api/v1/tenants/:serverName/suspend",
    async (request, reply) => {
      const { serverName } = request.params;

      const [tenant] = await db
        .select()
        .from(tenants)
        .where(eq(tenants.serverName, serverName));

      if (!tenant) {
        return reply.status(404).send({ error: `Tenant '${serverName}' not found` });
      }

      if (tenant.status !== "active") {
        return reply
          .status(400)
          .send({ error: `Cannot suspend tenant in '${tenant.status}' state` });
      }

      await db
        .update(tenants)
        .set({ status: "suspended", updatedAt: new Date() })
        .where(eq(tenants.serverName, serverName));

      try {
        await reloadSynapseTenants(config.SYNAPSE_URL, config.SYNAPSE_RELOAD_SECRET);
      } catch {
        // Non-fatal
      }

      return { server_name: serverName, status: "suspended" };
    }
  );

  // POST /api/v1/tenants/:serverName/activate
  app.post<{ Params: { serverName: string } }>(
    "/api/v1/tenants/:serverName/activate",
    async (request, reply) => {
      const { serverName } = request.params;

      const [tenant] = await db
        .select()
        .from(tenants)
        .where(eq(tenants.serverName, serverName));

      if (!tenant) {
        return reply.status(404).send({ error: `Tenant '${serverName}' not found` });
      }

      if (tenant.status !== "suspended") {
        return reply
          .status(400)
          .send({ error: `Cannot activate tenant in '${tenant.status}' state` });
      }

      await db
        .update(tenants)
        .set({ status: "active", updatedAt: new Date() })
        .where(eq(tenants.serverName, serverName));

      try {
        await reloadSynapseTenants(config.SYNAPSE_URL, config.SYNAPSE_RELOAD_SECRET);
      } catch {
        // Non-fatal
      }

      return { server_name: serverName, status: "active" };
    }
  );
}
