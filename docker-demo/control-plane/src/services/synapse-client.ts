/**
 * HTTP client for pushing reload notifications to Synapse.
 */

export interface ReloadResult {
  added: string[];
  removed: string[];
  unchanged: string[];
}

export async function reloadSynapseTenants(
  synapseUrl: string,
  reloadSecret: string
): Promise<ReloadResult> {
  const url = `${synapseUrl}/_synapse/admin/v1/tenants/reload`;

  const response = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${reloadSecret}`,
      "Content-Type": "application/json",
    },
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(
      `Synapse reload failed (${response.status}): ${body}`
    );
  }

  return (await response.json()) as ReloadResult;
}
