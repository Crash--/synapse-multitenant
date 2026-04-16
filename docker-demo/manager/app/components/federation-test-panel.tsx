"use client";

import { useState } from "react";

type Tenant = { server_name: string };
type StepEvent = { step: string; status: "running" | "ok" | "error"; detail?: unknown };

export function FederationTestPanel({
  source,
  others,
}: {
  source: Tenant;
  others: Tenant[];
}) {
  const [target, setTarget] = useState(others[0]?.server_name ?? "");
  const [steps, setSteps] = useState<StepEvent[]>([]);
  const [running, setRunning] = useState(false);
  const [verdict, setVerdict] = useState<"pending" | "pass" | "fail" | null>(null);

  async function runTest() {
    if (!target) return;
    setSteps([]);
    setVerdict("pending");
    setRunning(true);
    try {
      const res = await fetch(
        `/api/tenants/${encodeURIComponent(source.server_name)}/federation-test`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target }),
        }
      );
      if (!res.body) throw new Error("No response body");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
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
            setVerdict("pass");
          } else if (ev === "error") {
            setVerdict("fail");
          }
        }
      }
    } catch (_e) {
      setVerdict("fail");
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="border rounded p-4 space-y-3">
      <h3 className="font-semibold">Federation test</h3>
      <div className="flex gap-2 items-center flex-wrap">
        <span className="text-sm">
          From <code>{source.server_name}</code> to
        </span>
        <select
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          className="border rounded px-2 py-1"
        >
          <option value="">(select target)</option>
          {others.map((t) => (
            <option key={t.server_name} value={t.server_name}>
              {t.server_name}
            </option>
          ))}
        </select>
        <button
          onClick={runTest}
          disabled={running || !target}
          className="bg-blue-600 text-white rounded px-3 py-1 disabled:opacity-50"
        >
          {running ? "Running…" : "Run test"}
        </button>
      </div>
      {steps.length > 0 && (
        <ul className="space-y-1 text-sm">
          {steps.map((s) => (
            <li key={s.step} className="flex gap-2">
              <span className="w-5 text-center">
                {s.status === "running" && "⏳"}
                {s.status === "ok" && "✓"}
                {s.status === "error" && "✗"}
              </span>
              <span className="font-mono">{s.step}</span>
            </li>
          ))}
        </ul>
      )}
      {verdict === "pass" && (
        <div className="text-green-700 text-sm">
          ✓ Federation between {source.server_name} and {target} works end-to-end.
        </div>
      )}
      {verdict === "fail" && (
        <div className="text-red-700 text-sm">✗ Federation failed. See steps above.</div>
      )}
    </div>
  );
}
