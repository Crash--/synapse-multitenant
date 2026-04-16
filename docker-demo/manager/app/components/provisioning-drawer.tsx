"use client";

import { useEffect, useState } from "react";

type StepEvent = { step: string; status: "running" | "ok" | "error"; detail?: unknown };

type Props = {
  serverName: string;
  onClose: () => void;
  onComplete: (tenant: unknown) => void;
};

export function ProvisioningDrawer({ serverName, onClose, onComplete }: Props) {
  const [steps, setSteps] = useState<StepEvent[]>([]);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/tenants/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ server_name: serverName }),
        });
        if (!res.body) throw new Error("No response body");
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (!cancelled) {
          const { done: d, value } = await reader.read();
          if (d) break;
          buf += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf("\n\n")) !== -1) {
            const chunk = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            const lines = chunk.split("\n");
            const evLine = lines.find((l) => l.startsWith("event: "));
            const dataLine = lines.find((l) => l.startsWith("data: "));
            if (!evLine || !dataLine) continue;
            const ev = evLine.slice(7);
            const data = JSON.parse(dataLine.slice(6));
            if (ev === "step") {
              setSteps((s) => {
                const existing = s.findIndex((x) => x.step === data.step);
                if (existing >= 0) {
                  const next = [...s];
                  next[existing] = data;
                  return next;
                }
                return [...s, data];
              });
            } else if (ev === "complete") {
              setDone(true);
              onComplete(data.tenant);
            } else if (ev === "error") {
              setError(data.message || "Unknown error");
            }
          }
        }
      } catch (e: unknown) {
        setError((e as Error).message ?? String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [serverName, onComplete]);

  return (
    <div className="fixed inset-y-0 right-0 w-96 bg-white dark:bg-neutral-900 shadow-2xl p-6 overflow-y-auto border-l border-neutral-200 dark:border-neutral-800 z-50">
      <div className="flex justify-between items-center mb-4">
        <h2 className="font-semibold text-lg">Creating {serverName}</h2>
        <button
          onClick={onClose}
          className="text-neutral-500 hover:text-neutral-800"
          aria-label="Close"
        >
          ✕
        </button>
      </div>
      <ul className="space-y-2">
        {steps.map((s) => (
          <li key={s.step} className="flex items-center gap-2">
            <span className="w-5 text-center">
              {s.status === "running" && "⏳"}
              {s.status === "ok" && "✓"}
              {s.status === "error" && "✗"}
            </span>
            <span className="font-mono text-sm">{s.step}</span>
            {s.detail ? (
              <span className="text-xs text-neutral-500 ml-2 truncate">
                {typeof s.detail === "string" ? s.detail : JSON.stringify(s.detail).slice(0, 60)}
              </span>
            ) : null}
          </li>
        ))}
      </ul>
      {error && <div className="mt-4 text-red-600 text-sm">Error: {error}</div>}
      {done && <div className="mt-4 text-green-700">Tenant created.</div>}
    </div>
  );
}
