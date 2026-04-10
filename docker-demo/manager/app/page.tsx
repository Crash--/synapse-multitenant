"use client";

import { TenantDashboard } from "./components/tenant-dashboard";

export default function Home() {
  return (
    <div className="flex flex-col flex-1">
      <header className="border-b border-zinc-800 px-6 py-4">
        <div className="mx-auto flex max-w-5xl items-center justify-between">
          <h1 className="text-lg font-semibold tracking-tight">
            Synapse Multi-Tenant Manager
          </h1>
        </div>
      </header>

      <main className="flex-1 px-6 py-8">
        <div className="mx-auto max-w-5xl">
          <TenantDashboard />
        </div>
      </main>
    </div>
  );
}
