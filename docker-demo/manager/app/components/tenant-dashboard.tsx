"use client";

import { useCallback, useEffect, useState } from "react";
import { CreateTenantDialog } from "./create-tenant-dialog";

interface Tenant {
  id: string;
  server_name: string;
  database_schema: string;
  status: string;
  registration_enabled: boolean;
  enable_federation: boolean;
  max_mau_value: number;
  created_at: string;
}

export function TenantDashboard() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  const fetchTenants = useCallback(async () => {
    try {
      const res = await fetch("/api/tenants");
      if (!res.ok) throw new Error(`Failed to fetch tenants (${res.status})`);
      const data = await res.json();
      setTenants(data.tenants || []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch tenants");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTenants();
  }, [fetchTenants]);

  async function handleAction(serverName: string, action: string) {
    setActionLoading(serverName);
    setError(null);
    try {
      const res = await fetch("/api/tenants", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ server_name: serverName, action }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `${action} failed`);
      }
      await fetchTenants();
    } catch (err) {
      setError(err instanceof Error ? err.message : `${action} failed`);
    } finally {
      setActionLoading(null);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20 text-zinc-400">
        Loading tenants...
      </div>
    );
  }

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold">Tenants</h2>
        </div>
        <div className="flex gap-2">
          <button
            onClick={fetchTenants}
            className="rounded-lg border border-zinc-700 px-3 py-2 text-sm transition-colors hover:bg-zinc-800"
          >
            Refresh
          </button>
          <button
            onClick={() => setShowCreate(true)}
            className="rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-500"
          >
            Add Tenant
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-red-800 bg-red-950 px-3 py-2 text-sm text-red-300">
          {error}
          <button
            onClick={() => setError(null)}
            className="ml-2 text-red-400 hover:text-red-200"
          >
            dismiss
          </button>
        </div>
      )}

      {tenants.length === 0 ? (
        <div className="rounded-lg border border-zinc-800 py-12 text-center text-zinc-400">
          No tenants configured yet. Click &quot;Add Tenant&quot; to create one.
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-zinc-800">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-800 bg-zinc-900/50">
                <th className="px-4 py-3 text-left font-medium text-zinc-300">
                  Server Name
                </th>
                <th className="px-4 py-3 text-left font-medium text-zinc-300">
                  Schema
                </th>
                <th className="px-4 py-3 text-left font-medium text-zinc-300">
                  Status
                </th>
                <th className="px-4 py-3 text-left font-medium text-zinc-300">
                  Registration
                </th>
                <th className="px-4 py-3 text-left font-medium text-zinc-300">
                  Federation
                </th>
                <th className="px-4 py-3 text-right font-medium text-zinc-300">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody>
              {tenants.map((tenant) => (
                <tr
                  key={tenant.server_name}
                  className="border-b border-zinc-800/50 last:border-0"
                >
                  <td className="px-4 py-3">
                    <div className="font-medium">{tenant.server_name}</div>
                    <div className="text-xs text-zinc-500">
                      {new Date(tenant.created_at).toLocaleDateString()}
                    </div>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-zinc-400">
                    {tenant.database_schema}
                  </td>
                  <td className="px-4 py-3">
                    <TenantStatusBadge status={tenant.status} />
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge
                      enabled={tenant.registration_enabled}
                      label={tenant.registration_enabled ? "Open" : "Closed"}
                    />
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge
                      enabled={tenant.enable_federation}
                      label={tenant.enable_federation ? "Enabled" : "Disabled"}
                    />
                  </td>
                  <td className="px-4 py-3 text-right">
                    <TenantActions
                      tenant={tenant}
                      loading={actionLoading === tenant.server_name}
                      onAction={(action) =>
                        handleAction(tenant.server_name, action)
                      }
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showCreate && (
        <CreateTenantDialog
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            fetchTenants();
          }}
        />
      )}
    </div>
  );
}

function TenantActions({
  tenant,
  loading,
  onAction,
}: {
  tenant: Tenant;
  loading: boolean;
  onAction: (action: string) => void;
}) {
  if (loading) {
    return <span className="text-xs text-zinc-500">...</span>;
  }

  return (
    <div className="flex justify-end gap-1">
      {tenant.status === "active" && (
        <button
          onClick={() => onAction("suspend")}
          className="rounded px-2 py-1 text-xs text-amber-400 transition-colors hover:bg-zinc-800"
        >
          Suspend
        </button>
      )}
      {tenant.status === "suspended" && (
        <button
          onClick={() => onAction("activate")}
          className="rounded px-2 py-1 text-xs text-green-400 transition-colors hover:bg-zinc-800"
        >
          Activate
        </button>
      )}
      {(tenant.status === "active" || tenant.status === "suspended") && (
        <button
          onClick={() => {
            if (confirm(`Delete tenant ${tenant.server_name}?`)) {
              onAction("delete");
            }
          }}
          className="rounded px-2 py-1 text-xs text-red-400 transition-colors hover:bg-zinc-800"
        >
          Delete
        </button>
      )}
    </div>
  );
}

function TenantStatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    active: "bg-green-950 text-green-300",
    provisioning: "bg-blue-950 text-blue-300",
    suspended: "bg-amber-950 text-amber-300",
    deleting: "bg-red-950 text-red-300",
  };

  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
        styles[status] || "bg-zinc-800 text-zinc-400"
      }`}
    >
      {status}
    </span>
  );
}

function StatusBadge({
  enabled,
  label,
}: {
  enabled: boolean;
  label: string;
}) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
        enabled
          ? "bg-green-950 text-green-300"
          : "bg-zinc-800 text-zinc-400"
      }`}
    >
      {label}
    </span>
  );
}
