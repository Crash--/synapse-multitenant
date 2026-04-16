import { describe, it, expect, beforeAll, afterAll, vi } from "vitest";
import Fastify, { type FastifyInstance } from "fastify";
import { federationTestRoutes } from "./federation-test.js";

function makeDeps() {
  return {
    config: {
      SYNAPSE_URL: "http://synapse:8008",
      SHARED_SECRET: "test-shared-secret",
    },
  } as any;
}

describe("POST /api/v1/tenants/:source/federation-test", () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    (global as any).fetch = vi.fn(async (url: string, init: any) => {
      // Mock all Synapse calls to succeed.
      if (url.includes("/register")) {
        return {
          ok: true, status: 200,
          json: async () => ({ access_token: "fake-token", user_id: "@fake:demo" }),
          text: async () => JSON.stringify({ access_token: "fake-token", user_id: "@fake:demo" }),
        };
      }
      if (url.includes("/createRoom")) {
        return {
          ok: true, status: 200,
          json: async () => ({ room_id: "!test:demo" }),
          text: async () => JSON.stringify({ room_id: "!test:demo" }),
        };
      }
      if (url.includes("/sync")) {
        return {
          ok: true, status: 200,
          json: async () => ({
            rooms: {
              join: {
                "!test:demo": {
                  timeline: { events: [{ type: "m.room.message", content: { body: "ping" } }] },
                },
              },
            },
          }),
          text: async () => JSON.stringify({
            rooms: { join: { "!test:demo": { timeline: { events: [{ type: "m.room.message", content: { body: "ping" } }] } } } },
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({}), text: async () => "{}" };
    });
    app = Fastify();
    await app.register(federationTestRoutes, makeDeps());
    await app.ready();
  });

  afterAll(async () => {
    await app.close();
  });

  it("streams step events and completes", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/api/v1/tenants/acme.localhost/federation-test",
      headers: { accept: "text/event-stream" },
      payload: { target: "corp.localhost" },
    });
    expect(res.statusCode).toBe(200);
    expect(res.body).toMatch(/event: step/);
    expect(res.body).toMatch(/event: (complete|error)/);
  });

  it("rejects identical source and target", async () => {
    const res = await app.inject({
      method: "POST",
      url: "/api/v1/tenants/acme.localhost/federation-test",
      headers: { accept: "text/event-stream" },
      payload: { target: "acme.localhost" },
    });
    expect(res.statusCode).toBe(400);
  });
});
