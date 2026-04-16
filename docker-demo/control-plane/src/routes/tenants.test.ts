import { describe, it, expect, beforeAll, afterAll } from "vitest";
import Fastify, { type FastifyInstance } from "fastify";
import { tenantRoutes } from "./tenants.js";

function makeMockDeps() {
  const tenantRows: any[] = [];
  const db = {
    select: () => ({ from: () => ({ where: () => [] }) }),
    insert: () => ({
      values: (v: any) => ({
        returning: async () => [{ id: "t1", ...v }],
      }),
    }),
    update: () => ({
      set: () => ({ where: async () => undefined }),
    }),
  } as any;
  const config = {
    MEDIA_BASE_DIR: "/tmp/test-media",
    TENANT_KEY_MASTER: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    SYNAPSE_URL: "http://synapse:8008",
    SYNAPSE_RELOAD_SECRET: "test",
  };
  const pool = { query: async () => ({ rows: [] }), connect: async () => ({ query: async () => ({ rows: [] }), release: () => {} }) } as any;
  return { db, config, pool };
}

describe("POST /api/v1/tenants", () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    app = Fastify();
    await app.register(tenantRoutes, makeMockDeps() as any);
    await app.ready();
  });

  afterAll(async () => {
    await app.close();
  });

  it("returns SSE stream when Accept: text/event-stream", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/api/v1/tenants",
      headers: { accept: "text/event-stream" },
      payload: { server_name: "fresh-sse.localhost" },
    });
    expect(res.statusCode).toBe(200);
    expect(res.headers["content-type"]).toMatch(/text\/event-stream/);
    expect(res.body).toMatch(/event: step/);
  });

  it("returns single JSON when Accept: application/json", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/api/v1/tenants",
      headers: { accept: "application/json" },
      payload: { server_name: "fresh-json.localhost" },
    });
    expect(res.statusCode).toBe(201);
    expect(res.headers["content-type"]).toMatch(/application\/json/);
    const body = JSON.parse(res.body);
    expect(body.tenant.server_name).toBe("fresh-json.localhost");
  });

  it("rejects reserved names", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/api/v1/tenants",
      headers: { accept: "application/json" },
      payload: { server_name: "manager.localhost" },
    });
    expect(res.statusCode).toBe(400);
    expect(JSON.parse(res.body).error).toMatch(/reserved/i);
  });
});
