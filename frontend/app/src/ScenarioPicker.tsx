import { useEffect, useState } from "react";
import { api } from "./api";
import { useToast } from "./toast";

// §5: run ANY scenario — grouped by protocol family, dry-run preview of
// the exact per-target lines before committing the fleet to it.

interface Tpl { name: string; protocol: string; description: string; convergence_timeout: number }

export default function ScenarioPicker({ onClose, onStarted }: { onClose: () => void; onStarted: (run: number) => void }) {
  const [scenarios, setScenarios] = useState<Tpl[]>([]);
  const [preview, setPreview] = useState<Record<string, string> | null>(null);
  const [previewOf, setPreviewOf] = useState("");
  const [busy, setBusy] = useState("");
  const toast = useToast();

  useEffect(() => {
    api.runs; // noop touch
    fetch("/api/scenarios").then((r) => r.json()).then(setScenarios).catch((e) => toast.fromError("scenarios failed to load", e));
  }, []);

  const dryRun = async (name: string) => {
    setBusy(name);
    try {
      const r = await api.renderScenario(name);
      setPreview(r.targets);
      setPreviewOf(name);
    } catch (e) {
      toast.fromError(`dry-run of ${name} failed`, e);
    } finally {
      setBusy("");
    }
  };

  const run = async (name: string) => {
    setBusy(name);
    try {
      const r = await api.startRun(name) as Record<string, unknown>;
      toast.ok(`run #${r.run} started`, name);
      onStarted(Number(r.run));
      onClose();
    } catch (e) {
      toast.fromError(`${name} failed to start`, e);
    } finally {
      setBusy("");
    }
  };

  const groups = scenarios.reduce<Record<string, Tpl[]>>((m, s) => {
    (m[s.protocol.toUpperCase()] ??= []).push(s);
    return m;
  }, {});

  return (
    <div className="fixed inset-0 z-40 grid place-items-center bg-black/80 p-4" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="max-h-[80vh] w-full max-w-2xl overflow-y-auto rounded-[0.25rem] border border-edge bg-card p-4" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center justify-between">
          <span className="font-mono text-sm text-accent-soft">run scenario</span>
          <button onClick={onClose} aria-label="close" className="font-mono text-xs text-dim-neutral hover:text-text">esc ✕</button>
        </div>
        {Object.entries(groups).map(([proto, items]) => (
          <div key={proto} className="mb-3">
            <div className="mb-1 font-mono text-[10px] uppercase tracking-widest text-dim-neutral">{proto}</div>
            {items.map((s) => (
              <div key={s.name} className="flex items-center justify-between gap-3 rounded-[0.25rem] border border-edge/60 px-3 py-2">
                <div className="min-w-0">
                  <div className="font-mono text-xs text-accent-soft">{s.name}</div>
                  <div className="truncate text-[11px] text-dim-neutral">{s.description}</div>
                  <div className="font-mono text-[10px] text-dimmer-neutral">timeout {s.convergence_timeout}s</div>
                </div>
                <div className="flex shrink-0 gap-1">
                  <button
                    onClick={() => dryRun(s.name)}
                    disabled={busy !== ""}
                    className="rounded-[0.25rem] border border-secondary/40 px-2 py-1 font-mono text-[10px] text-secondary hover:bg-secondary/10 disabled:opacity-40"
                    aria-label={`dry-run ${s.name}`}
                  >
                    {busy === s.name && previewOf !== s.name ? "…" : "⚙ dry-run"}
                  </button>
                  <button
                    onClick={() => run(s.name)}
                    disabled={busy !== ""}
                    className="rounded-[0.25rem] bg-accent/10 px-3 py-1 font-mono text-[10px] text-accent hover:bg-accent/20 disabled:opacity-40"
                    aria-label={`run ${s.name}`}
                  >
                    ▶ run
                  </button>
                </div>
              </div>
            ))}
          </div>
        ))}
        {preview && (
          <div className="mt-2">
            <div className="mb-1 font-mono text-[10px] uppercase tracking-widest text-dim-neutral">
              dry-run: {previewOf} — {Object.keys(preview).length} targets (nothing pushed)
            </div>
            <div className="grid max-h-60 gap-1 overflow-y-auto">
              {Object.entries(preview).map(([node, cfg]) => (
                <details key={node} className="rounded-[0.25rem] border border-edge">
                  <summary className="cursor-pointer px-2 py-1 font-mono text-[11px] text-accent-soft">{node} · {cfg.splitlines().length} lines</summary>
                  <pre className="max-h-40 overflow-auto bg-black px-2 py-1 font-mono text-[10px] leading-4 text-dim-neutral">{cfg}</pre>
                </details>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
