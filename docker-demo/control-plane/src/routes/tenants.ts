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

    // Check for duplicates
    const [existing] = await db
      .select()
      .from(tenants)
      .where(eq(tenants.serverName, serverName));

    if (existing) {
      return reply
        .status(409)
        .send({ error: `Tenant '${serverName}' already exists` });
    }

    const safeName = serverName.replace(/[^a-zA-Z0-9]/g, "_");
    const databaseSchema = `tenant_${safeName}`;
    const mediaStorePath = `${config.MEDIA_BASE_DIR}/${serverName}`;

    // Step 1: Generate signing key
    const { keyText, keyId } = generateSigningKey(serverName);
    const signingKeyEncrypted = encryptSigningKey(keyText, masterKey);

    // Step 2: Generate secrets
    const macaroonSecretKey = randomBytes(32).toString("hex");
    const formSecret = randomBytes(32).toString("hex");

    // Step 3: Insert with status=provisioning
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
        publicBaseurl: body.public_baseurl ?? `http://${serverName}/`,
      })
      .returning();

    const steps: Array<{ step: string; status: string; detail?: string }> = [];
    steps.push({ step: "insert_tenant_row", status: "done" });

    // Step 4: Clone DB schema from public
    try {
      await provisionTenantSchema(pool, databaseSchema);
      steps.push({ step: "clone_schema", status: "done" });
    } catch (err: any) {
      steps.push({
        step: "clone_schema",
        status: "failed",
        detail: err.message,
      });
      return reply.status(500).send({
        error: "Schema provisioning failed",
        tenant: { server_name: serverName, status: "provisioning" },
        steps,
      });
    }

    // Step 5: Create media directory
    try {
      const { mkdir } = await import("node:fs/promises");
      await mkdir(mediaStorePath, { recursive: true });
      steps.push({ step: "create_media_dir", status: "done" });
    } catch (err: any) {
      steps.push({
        step: "create_media_dir",
        status: "failed",
        detail: err.message,
      });
      // Non-fatal — Synapse will create it on first upload
    }

    // Step 6: Activate
    await db
      .update(tenants)
      .set({ status: "active", updatedAt: new Date() })
      .where(eq(tenants.id, inserted.id));
    steps.push({ step: "activate", status: "done" });

    // Step 7: Push reload to Synapse
    try {
      const reloadResult = await reloadSynapseTenants(
        config.SYNAPSE_URL,
        config.SYNAPSE_RELOAD_SECRET
      );
      steps.push({
        step: "synapse_reload",
        status: "done",
        detail: `added=${reloadResult.added.length}`,
      });
    } catch (err: any) {
      steps.push({
        step: "synapse_reload",
        status: "failed",
        detail: err.message,
      });
      // Non-fatal — Synapse will pick up on next restart
    }

    return reply.status(201).send({
      tenant: {
        id: inserted.id,
        server_name: serverName,
        database_schema: databaseSchema,
        status: "active",
      },
      steps,
    });
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
