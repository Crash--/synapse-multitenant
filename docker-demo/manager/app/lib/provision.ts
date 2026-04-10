/**
 * Tenant provisioning via the control plane API.
 *
 * All tenant lifecycle operations are delegated to the control plane
 * service, which handles key generation, schema cloning, DB persistence,
 * and Synapse reload.
 */

const CONTROL_PLANE_URL =
  process.env.CONTROL_PLANE_URL || "http://control-plane:3001";
const CONTROL_PLANE_TOKEN =
  process.env.CONTROL_PLANE_TOKEN || "demo-control-plane-token";

function headers(): Record<string, string> {
  return {
    Authorization: `Bearer ${CONTROL_PLANE_TOKEN}`,
    "Content-Type": "application/json",
  };
}

export interface ProvisionStep {
  step: string;
  status: string;
  detail?: string;
}

export interface ProvisionResult {
  steps: ProvisionStep[];
  success: boolean;
  tenant?: {
    id: string;
    server_name: string;
    database_schema: string;
    status: string;
  };
}

export interface TenantSummary {
  id: string;
  server_name: string;
  database_schema: string;
  status: string;
  registration_enabled: boolean;
  enable_federation: boolean;
  max_mau_value: number;
  created_at: string;
}

export async function provisionTenant(
  serverName: string,
  opts: {
    registrationEnabled?: boolean;
    federationEnabled?: boolean;
  } = {}
): Promise<ProvisionResult> {
  const res = await fetch(`${CONTROL_PLANE_URL}/api/v1/tenants`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      server_name: serverName,
      registration_enabled: opts.registrationEnabled ?? true,
      enable_federation: opts.federationEnabled ?? false,
    }),
  });

  const data = await res.json();

  if (!res.ok) {
    return {
      steps: data.steps || [
        { step: "Control plane request", status: "error", detail: data.error },
      ],
      success: false,
    };
  }

  return {
    steps: data.steps || [],
    success: true,
    tenant: data.tenant,
  };
}

export async function listTenants(): Promise<TenantSummary[]> {
  const res = await fetch(`${CONTROL_PLANE_URL}/api/v1/tenants`, {
    headers: headers(),
  });

  if (!res.ok) {
    throw new Error(`Control plane returned ${res.status}`);
  }

  const data = await res.json();
  return data.tenants || [];
}

export async function suspendTenant(serverName: string): Promise<void> {
  const res = await fetch(
    `${CONTROL_PLANE_URL}/api/v1/tenants/${encodeURIComponent(serverName)}/suspend`,
    { method: "POST", headers: headers() }
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Suspend failed (${res.status})`);
  }
}

export async function activateTenant(serverName: string): Promise<void> {
  const res = await fetch(
    `${CONTROL_PLANE_URL}/api/v1/tenants/${encodeURIComponent(serverName)}/activate`,
    { method: "POST", headers: headers() }
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Activate failed (${res.status})`);
  }
}

export async function deleteTenant(serverName: string): Promise<void> {
  const res = await fetch(
    `${CONTROL_PLANE_URL}/api/v1/tenants/${encodeURIComponent(serverName)}`,
    { method: "DELETE", headers: headers() }
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Delete failed (${res.status})`);
  }
}
