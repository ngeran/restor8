import { useEffect, useState } from "react";
import { api, type Run } from "./api";

// §5: full run anatomy — phase timeline, per-node convergence, JSNAPy
// pre/post outcomes per device (the point of snapshotting both sides).

interface Detail {
  phases?: { phase: string; [k: string]: unknown }[];
  nodes?: Record<string, { pushed?: boolean; established?: number; peers?: number; expected?: number }>;
  jsnapy?: Record<string, { passed: boolean; results: { test: string; result: string }[] }>;
  error?: string;
}

export default function RunDetail({ runId, onClose }: { runId: number; onClose: () => void }) {
  const [run, setRun] = useState<Run | null>(null);
  const [detail, setDetail] = useState<Detail>({});

  useEffect(() => {
    let live = true;
    const load = () =>
      api.run(runId)
        .then((r) => { if (live) { setRun(r); setDetail((r.detail as Detail) ?? {}); } })
        .catch(() => {});
    load();
    const t = setInterval(load, run?.status === "running" ? 4000 : 0);
    return () => { live = false; clearInterval(t); };
  }, [runId, run?.status]);

  return (
    <div className="fixed inset-0 z-40 grid place-items-center bg-black/80 p-4" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="max-h-[80vh] w-full max-w-2xl overflow-y-auto rounded-[0.25rem] border border-edge bg-card p-4" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center justify-between">
          <span className="font-mono text-sm text-accent-soft">
            run #{runId} {run?.scenario && <span className="text-dim-neutral">{run.scenario}</span>}
          </span>
          <span className={`font-mono text-xs ${run?.status === "passed" ? "text-ok" : run?.status === "failed" ? "text-err" : "text-warn"}`}>
            {run?.status ?? "…"}
          </span>
        </div>
        {detail.error && <div className="mb-2 rounded-[0.25rem] border border-err/40 p-2 font-mono text-[11px] text-err">{detail.error}</div>}
        {detail.phases && (
          <div className="mb-3 grid gap-1">
            {detail.phases.map((p, i) => (
              <div key={i} className="flex gap-2 font-mono text-[11px]">
                <span className="w-32 shrink-0 text-secondary">{p.phase}</span>
                <span className="text-dim-neutral">{JSON.stringify(Object.fromEntries(Object.entries(p).filter(([k]) => k !== "phase"))).slice(0, 120)}</span>
              </div>
            ))}
          </div>
        )}
        {detail.nodes && (
          <table className="mb-3 w-full font-mono text-[11px]">
            <thead><tr className="text-left text-[10px] uppercase text-dimmer-neutral"><th className="py-1">node</th><th>peers</th><th>jsnapy</th></tr></thead>
            <tbody>
              {Object.entries(detail.nodes).map(([n, v]) => (
                <tr key={n} className="border-t border-edge/40">
                  <td className="py-1 text-accent-soft">{n}</td>
                  <td className={v.expected && v.established !== undefined && v.established >= v.expected ? "text-ok" : "text-warn"}>
                    {v.established ?? "-"}/{v.peers ?? "-"} <span className="text-dimmer-neutral">exp {v.expected ?? "?"}</span>
                  </td>
                  <td className={detail.jsnapy?.[n]?.passed ? "text-ok" : "text-err"}>
                    {detail.jsnapy?.[n]?.passed ? "passed" : detail.jsnapy?.[n] ? "failed" : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
