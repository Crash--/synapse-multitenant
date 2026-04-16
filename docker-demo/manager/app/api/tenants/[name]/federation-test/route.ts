import type { NextRequest } from "next/server";

const CONTROL_PLANE_URL = process.env.CONTROL_PLANE_URL || "http://control-plane:3001";
const CONTROL_PLANE_TOKEN = process.env.CONTROL_PLANE_TOKEN || "";

export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;
  const body = await req.text();
  const upstream = await fetch(
    `${CONTROL_PLANE_URL}/api/v1/tenants/${encodeURIComponent(name)}/federation-test`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        Authorization: `Bearer ${CONTROL_PLANE_TOKEN}`,
      },
      body,
    }
  );
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  });
}
