import type { NextRequest } from "next/server";
import { seedTenantBranch } from "@/app/lib/ldap";
import { applyTenantOidcConfig } from "@/app/lib/oidc-provision";

const CONTROL_PLANE_URL = process.env.CONTROL_PLANE_URL || "http://control-plane:3001";
const CONTROL_PLANE_TOKEN = process.env.CONTROL_PLANE_TOKEN || "";

export async function POST(req: NextRequest) {
  const body = await req.text();
  const parsed = JSON.parse(body) as { server_name: string };
  const serverName = parsed.server_name;

  const upstream = await fetch(`${CONTROL_PLANE_URL}/api/v1/tenants`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      Authorization: `Bearer ${CONTROL_PLANE_TOKEN}`,
    },
    body,
  });

  if (!upstream.ok || !upstream.body) {
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
    });
  }

  const encoder = new TextEncoder();
  const readable = new ReadableStream({
    async start(controller) {
      const reader = upstream.body!.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let cpCompleted = false;
      let cpTenant: unknown = null;

      const emit = (event: string, data: unknown) => {
        controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      };

      const forwardRaw = (chunk: Uint8Array) => controller.enqueue(chunk);

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          forwardRaw(value);
          buf += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf("\n\n")) !== -1) {
            const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
            const lines = chunk.split("\n");
            const evLine = lines.find((l) => l.startsWith("event: "));
            const dataLine = lines.find((l) => l.startsWith("data: "));
            if (!evLine || !dataLine) continue;
            const ev = evLine.slice(7);
            const data = JSON.parse(dataLine.slice(6));
            if (ev === "complete") { cpCompleted = true; cpTenant = data.tenant; }
          }
        }

        if (!cpCompleted) {
          controller.close();
          return;
        }

        try {
          emit("step", { step: "ldap_branch", status: "running" });
          const seed = await seedTenantBranch(serverName);
          emit("step", { step: "ldap_branch", status: "ok", detail: { branch_dn: seed.branch_dn } });

          emit("step", { step: "seed_users", status: "running" });
          emit("step", { step: "seed_users", status: "ok", detail: { count: seed.users.length, users: seed.users.map(u => u.uid) } });

          emit("step", { step: "oidc_config", status: "running" });
          await applyTenantOidcConfig(serverName);
          emit("step", { step: "oidc_config", status: "ok" });

          emit("complete", { tenant: cpTenant, sso: { login_url: `https://${serverName}/_matrix/client/v3/login`, demo_users: ["alice", "bob", "charlie"], password: "demo" } });
        } catch (e: unknown) {
          emit("error", { message: (e as Error).message });
        }
      } finally {
        controller.close();
      }
    },
  });

  return new Response(readable, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  });
}
