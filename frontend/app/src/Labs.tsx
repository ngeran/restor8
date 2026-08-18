import { useEffect, useState } from "react";
import { api, type Lab } from "./api";
import { useEvents } from "./events";

// Lab = the full-fleet configuration for one exercise, grouped by family
// (MPLS, L3VPN, TWAMP…). Applying is idempotent — re-apply IS the restore.

interface NodeRes { device: string; ok: boolean; diff_lines: number; error: string }

export default function Labs() {
  const [labs, setLabs] = useState<Lab[]>([]);
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState<{ lab: string; applied: number; failed: number; nodes: NodeRes[] } | null>(null);
  const { events, state } = useEvents();

  useEffect(() => {
    api.labs().then(setLabs).catch(() => {});
  }, []);

  const apply = async (name: string) => {
    setBusy(name);
    setResult(null);
    try {
      setResult(await api.applyLab(name) as never);
    } catch (e) {
      setResult({ lab: name, applied: 0, failed: 1, nodes: [{ device: "-", ok: false, diff_lines: 0, error: String(e) }] });
    } finally {
      setBusy("");
    }
  };

  const groups = labs.reduce<Record<string, Lab[]>>((m, l) => {
    (m[l.group] ??= []).push(l);
    return m;
  }, {});

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_360px]">
      <div className="grid content-start gap-4">
        {Object.entries(groups).map(([group, items]) => (
          <section key={group} className="rounded-[0.25rem] border border-edge bg-card">
            <div className="border-b border-edge px-4 py-2 font-mono text-[10px] uppercase tracking-widest text-dim-neutral">
              {group}
            </div>
            {items.map((l) => (
              <div key={l.name} className="flex items-center justify-between gap-3 border-b border-edge/40 px-4 py-3 last:border-0">
                <div>
                  <div className="font-mono text-xs text-accent-soft">{l.name}</div>
                  <div className="text-[11px] text-dim-neutral">{l.description}</div>
                </div>
                <button
                  onClick={() => apply(l.name)}
                  disabled={busy !== ""}
                  className={`shrink-0 rounded-[0.25rem] px-3 py-1 font-mono text-xs ${busy === l.name ? "text-warn" : "bg-accent/10 text-accent hover:bg-accent/20"} disabled:opacity-50`}
                >
                  {busy === l.name ? "applying…" : "▶ apply / restore"}
                </button>
              </div>
            ))}
          </section>
        ))}
        {labs.length === 0 && (
          <div className="rounded-[0.25rem] border border-edge bg-card p-4 font-mono text-xs text-dimmer-neutral">no labs defined</div>
        )}

        {result && (
          <section className="rounded-[0.25rem] border border-edge bg-card p-4">
            <div className="mb-2 font-mono text-xs">
              <span className="text-accent-soft">{result.lab}</span>
              <span className={result.failed ? " text-err" : " text-ok"}> — {result.applied} ok / {result.failed} failed</span>
            </div>
            <div className="grid max-h-64 gap-0.5 overflow-y-auto font-mono text-[11px]">
              {result.nodes.map((n) => (
                <div key={n.device} className="flex gap-3">
                  <span className={n.ok ? "text-ok" : "text-err"}>{n.ok ? "✓" : "✗"}</span>
                  <span className="w-16 text-accent-soft">{n.device}</span>
                  <span className="text-dim-neutral">{n.ok ? `+/-${n.diff_lines} lines` : n.error.slice(0, 90)}</span>
                </div>
              ))}
            </div>
          </section>
        )}
      </div>

      <section className="rounded-[0.25rem] border border-edge bg-card p-4">
        <h2 className="mb-3 font-mono text-xs uppercase tracking-widest text-dim-neutral">
          live events {state.kind === "live" ? <span className="text-ok">●</span> : <span className="text-warn">◌ {state.inSecs}s</span>}
        </h2>
        <div className="grid max-h-[70vh] gap-0.5 overflow-y-auto font-mono text-[11px]">
          {[...events].reverse().slice(0, 60).map((e, i) => (
            <div key={i} className="flex gap-2">
              <span className="w-28 shrink-0 truncate text-secondary">{e.device ?? `run#${e.run ?? "?"}`}</span>
              <span className="text-accent-soft">{e.stage ?? e.phase ?? ""}</span>
            </div>
          ))}
          {events.length === 0 && <div className="py-4 text-center text-dimmer-neutral">waiting…</div>}
        </div>
      </section>
    </div>
  );
}
