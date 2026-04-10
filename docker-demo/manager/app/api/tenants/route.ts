import { NextRequest, NextResponse } from "next/server";
import {
  provisionTenant,
  listTenants,
  suspendTenant,
  activateTenant,
  deleteTenant,
} from "@/app/lib/provision";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const tenants = await listTenants();
    return NextResponse.json({ tenants });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to list tenants" },
      { status: 502 }
    );
  }
}

export async function POST(request: NextRequest) {
  const body = await request.json();
  const { server_name, registration_enabled, federation_enabled } = body;

  if (!server_name || typeof server_name !== "string") {
    return NextResponse.json(
      { error: "server_name is required" },
      { status: 400 }
    );
  }

  const result = await provisionTenant(server_name, {
    registrationEnabled: registration_enabled ?? true,
    federationEnabled: federation_enabled ?? false,
  });

  return NextResponse.json(result, {
    status: result.success ? 201 : 500,
  });
}

export async function PATCH(request: NextRequest) {
  const body = await request.json();
  const { server_name, action } = body;

  if (!server_name) {
    return NextResponse.json(
      { error: "server_name is required" },
      { status: 400 }
    );
  }

  try {
    if (action === "suspend") {
      await suspendTenant(server_name);
    } else if (action === "activate") {
      await activateTenant(server_name);
    } else if (action === "delete") {
      await deleteTenant(server_name);
    } else {
      return NextResponse.json(
        { error: "Invalid action. Use suspend, activate, or delete." },
        { status: 400 }
      );
    }
    return NextResponse.json({ ok: true });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Action failed" },
      { status: 502 }
    );
  }
}
