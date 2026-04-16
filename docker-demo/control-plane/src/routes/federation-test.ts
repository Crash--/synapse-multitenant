import type { FastifyInstance } from "fastify";
import type { Config } from "../config.js";

type Deps = { config: Config & { SHARED_SECRET?: string } };

export async function federationTestRoutes(app: FastifyInstance, opts: Deps) {
  const { config } = opts;

  app.post<{
    Params: { source: string };
    Body: { target: string };
  }>("/api/v1/tenants/:source/federation-test", async (request, reply) => {
    const { source } = request.params;
    const { target } = request.body;
    const baseUrl = config.SYNAPSE_URL;

    if (!target || typeof target !== "string") {
      return reply.status(400).send({ error: "Missing target" });
    }
    if (target === source) {
      return reply.status(400).send({ error: "target must differ from source" });
    }

    reply.raw.setHeader("Content-Type", "text/event-stream");
    reply.raw.setHeader("Cache-Control", "no-cache");
    reply.raw.setHeader("Connection", "keep-alive");
    reply.raw.flushHeaders();

    const emit = (event: string, data: unknown) => {
      reply.raw.write(`event: ${event}\n`);
      reply.raw.write(`data: ${JSON.stringify(data)}\n\n`);
    };

    const stamp = Date.now();
    // Matrix reserves leading underscores for appservice-owned users
    // (M_INVALID_USERNAME). Use a plain prefix instead.
    const sourceUser = `fedtest-src-${stamp}`;
    const targetUser = `fedtest-tgt-${stamp}`;
    let sourceToken = "";
    let targetToken = "";
    let roomId = "";

    try {
      emit("step", { step: "register_source", status: "running" });
      sourceToken = await registerUser(baseUrl, source, sourceUser);
      emit("step", { step: "register_source", status: "ok" });

      emit("step", { step: "register_target", status: "running" });
      targetToken = await registerUser(baseUrl, target, targetUser);
      emit("step", { step: "register_target", status: "ok" });

      emit("step", { step: "create_room", status: "running" });
      roomId = await createRoom(baseUrl, source, sourceToken);
      emit("step", { step: "create_room", status: "ok", detail: { roomId } });

      emit("step", { step: "invite", status: "running" });
      await invite(baseUrl, source, sourceToken, roomId, `@${targetUser}:${target}`);
      emit("step", { step: "invite", status: "ok" });

      emit("step", { step: "join", status: "running" });
      await joinRoom(baseUrl, target, targetToken, roomId, [source]);
      emit("step", { step: "join", status: "ok" });

      emit("step", { step: "send_message", status: "running" });
      await sendMessage(baseUrl, source, sourceToken, roomId, "ping");
      emit("step", { step: "send_message", status: "ok" });

      emit("step", { step: "receive_message", status: "running" });
      const received = await pollForMessage(baseUrl, target, targetToken, roomId, "ping", 8000);
      emit("step", { step: "receive_message", status: received ? "ok" : "error" });
      if (!received) throw new Error("Message not received on target within 8s");

      emit("complete", { ok: true, roomId });
    } catch (err: any) {
      emit("error", { message: err.message });
    } finally {
      reply.raw.end();
    }
    return reply;
  });
}

// --- Helpers ---

async function fetchJson(url: string, host: string, init: any): Promise<any> {
  const headers = {
    "Content-Type": "application/json",
    Host: host,
    ...(init.headers || {}),
  };
  const resp = await fetch(url, { ...init, headers });
  const text = await resp.text();
  if (!resp.ok) {
    throw new Error(`${init.method || "GET"} ${url} → ${resp.status}: ${text}`);
  }
  return text ? JSON.parse(text) : {};
}

async function registerUser(base: string, host: string, localpart: string): Promise<string> {
  const body = {
    auth: { type: "m.login.dummy" },
    username: localpart,
    password: "fed-test-password",
  };
  const res = await fetchJson(`${base}/_matrix/client/v3/register`, host, {
    method: "POST",
    body: JSON.stringify(body),
  });
  return res.access_token;
}

async function createRoom(base: string, host: string, token: string): Promise<string> {
  const res = await fetchJson(`${base}/_matrix/client/v3/createRoom`, host, {
    method: "POST",
    body: JSON.stringify({ preset: "private_chat", name: "fed-test" }),
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.room_id;
}

async function invite(base: string, host: string, token: string, roomId: string, userId: string): Promise<void> {
  await fetchJson(
    `${base}/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/invite`,
    host,
    {
      method: "POST",
      body: JSON.stringify({ user_id: userId }),
      headers: { Authorization: `Bearer ${token}` },
    }
  );
}

async function joinRoom(base: string, host: string, token: string, roomId: string, via: string[]): Promise<void> {
  const qs = via.map((v) => `server_name=${encodeURIComponent(v)}`).join("&");
  await fetchJson(
    `${base}/_matrix/client/v3/join/${encodeURIComponent(roomId)}?${qs}`,
    host,
    {
      method: "POST",
      body: "{}",
      headers: { Authorization: `Bearer ${token}` },
    }
  );
}

async function sendMessage(base: string, host: string, token: string, roomId: string, body: string): Promise<void> {
  const txn = Date.now().toString();
  await fetchJson(
    `${base}/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/send/m.room.message/${txn}`,
    host,
    {
      method: "PUT",
      body: JSON.stringify({ msgtype: "m.text", body }),
      headers: { Authorization: `Bearer ${token}` },
    }
  );
}

async function pollForMessage(
  base: string, host: string, token: string, roomId: string, text: string, timeoutMs: number
): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetchJson(`${base}/_matrix/client/v3/sync?timeout=2000`, host, {
        method: "GET",
        headers: { Authorization: `Bearer ${token}` },
      });
      const room = res.rooms?.join?.[roomId];
      const events = room?.timeline?.events ?? [];
      if (events.some((e: any) => e.type === "m.room.message" && e.content?.body === text)) {
        return true;
      }
    } catch (_) {
      // ignore and retry
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}
