"use client";

import { useState } from "react";
import { ProvisioningDrawer } from "./provisioning-drawer";

interface ProvisionStep {
  step: string;
  status: string;
  detail?: string;
}

interface CreateTenantDialogProps {
  onClose: () => void;
  onCreated: () => void;
}

export function CreateTenantDialog({
  onClose,
  onCreated,
}: CreateTenantDialogProps) {
  const [serverName, setServerName] = useState("");
  const [registrationEnabled, setRegistrationEnabled] = useState(true);
  const [federationEnabled, setFederationEnabled] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [steps, setSteps] = useState<ProvisionStep[] | null>(null);
  const [creatingName, setCreatingName] = useState<string | null>(null);

  const schema = serverName
    ? `tenant_${serverName.replace(/[.\-]/g, "_")}`
    : "";

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSteps(null);
    setCreatingName(serverName);
  }

  const done = steps?.every((s) => s.status !== "error" && s.status !== "failed");

  return (
    <>
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-lg rounded-xl border border-zinc-800 bg-zinc-900 p-6 shadow-2xl">
        <h3 className="mb-4 text-lg font-semibold">Add Tenant</h3>

        {steps ? (
          // Show provisioning progress
          <div className="flex flex-col gap-3">
            {steps.map((s, i) => (
              <div
                key={i}
                className="flex items-start gap-3 rounded-lg bg-zinc-800/50 px-3 py-2 text-sm"
              >
                <span className="mt-0.5">
                  {(s.status === "ok" || s.status === "done") && (
                    <span className="text-green-400">&#10003;</span>
                  )}
                  {s.status === "skipped" && (
                    <span className="text-zinc-500">&#8211;</span>
                  )}
                  {(s.status === "error" || s.status === "failed") && (
                    <span className="text-red-400">&#10007;</span>
                  )}
                </span>
                <div>
                  <div className="font-medium text-zinc-200">{s.step}</div>
                  {s.detail && (
                    <div className="text-xs text-zinc-500">{s.detail}</div>
                  )}
                </div>
              </div>
            ))}

            {done && (
              <div className="mt-2 rounded-lg border border-green-800 bg-green-950 px-3 py-2 text-sm text-green-300">
                Tenant provisioned successfully. Synapse has been notified to
                reload.
              </div>
            )}

            {error && (
              <div className="rounded-lg border border-red-800 bg-red-950 px-3 py-2 text-sm text-red-300">
                {error}
              </div>
            )}

            <div className="mt-2 flex justify-end">
              <button
                onClick={done ? onCreated : onClose}
                className="rounded-lg border border-zinc-700 px-4 py-2 text-sm transition-colors hover:bg-zinc-800"
              >
                {done ? "Done" : "Close"}
              </button>
            </div>
          </div>
        ) : (
          // Show form
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <label className="flex flex-col gap-1.5">
              <span className="text-sm font-medium text-zinc-300">
                Server Name
              </span>
              <input
                type="text"
                value={serverName}
                onChange={(e) => setServerName(e.target.value)}
                placeholder="matrix.example.com"
                required
                className="rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm placeholder:text-zinc-500 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
              />
            </label>

            {serverName && (
              <div className="rounded-lg bg-zinc-800/50 px-3 py-2 text-xs text-zinc-400">
                <div>
                  Schema:{" "}
                  <span className="font-mono text-zinc-300">{schema}</span>
                </div>
                <div>
                  Media:{" "}
                  <span className="font-mono text-zinc-300">
                    /media/{serverName}
                  </span>
                </div>
              </div>
            )}

            <div className="flex gap-6">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={registrationEnabled}
                  onChange={(e) => setRegistrationEnabled(e.target.checked)}
                  className="rounded border-zinc-600 bg-zinc-800"
                />
                <span className="text-zinc-300">Open registration</span>
              </label>

              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={federationEnabled}
                  onChange={(e) => setFederationEnabled(e.target.checked)}
                  className="rounded border-zinc-600 bg-zinc-800"
                />
                <span className="text-zinc-300">Federation</span>
              </label>
            </div>

            <p className="text-xs text-zinc-500">
              This will: generate a signing key, create the DB schema, clone
              table structure, create the media directory, and push a reload
              to Synapse via the control plane.
            </p>

            {error && (
              <div className="rounded-lg border border-red-800 bg-red-950 px-3 py-2 text-sm text-red-300">
                {error}
              </div>
            )}

            <div className="mt-2 flex justify-end gap-2">
              <button
                type="button"
                onClick={onClose}
                className="rounded-lg border border-zinc-700 px-4 py-2 text-sm transition-colors hover:bg-zinc-800"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={!serverName || creatingName !== null}
                className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {creatingName ? "Provisioning..." : "Create Tenant"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
    {creatingName && (
      <ProvisioningDrawer
        serverName={creatingName}
        onClose={() => setCreatingName(null)}
        onComplete={() => {
          onCreated();
          setCreatingName(null);
        }}
      />
    )}
    </>
  );
}
