import { useEffect, useState } from "react";
import { api, type Lab } from "./api";
import { useEvents } from "./events";
import { useToast } from "./toast";
import { ConfirmModal, Skeleton } from "./ui";
import { useResource } from "./resource";

// Lab = the full-fleet configuration for one exercise, grouped by family
// (MPLS, L3VPN, TWAMP…). Applying is idempotent — re-apply IS the restore.

interface NodeRes { device: string; ok: boolean; diff_lines: number; error: string }

export default function Labs() {
  const [labs, setLabs] = useState<Lab[]>([]);
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState<{ lab: string; applied: number; failed: number; nodes: NodeRes[] } | null>(null);
  const { events, state } = useEvents();
  const toast = useToast();

  const [loadTick, setLoadTick] = useState(0);
  const snapsQ = useResource("snapshots", api.snapshots);
  const snapshots = snapsQ.data ?? [];
  const loadTickReload = () => snapsQ.reload();
  useEffect(() => {
    let live = true;
    api.labs()
      .then((l) => { if (live) setLabs(l); })
      .catch((e) => { if (live) toast.fromError("labs failed to load", e); });
    return () => { live = false; };
  }, [loadTick]);

  const apply = async (name: string) => {
    setBusy(name);
    setResult(null);
    try {
      const r = await api.applyLab(name) as { lab: string; applied: number; failed: number };
      setResult(r as never);
      if (r.failed) toast.warn(`${name}: ${r.failed} node(s) failed`, "see per-node results");
      else toast.ok(`${name} applied`, `${r.applied} nodes`);
    } catch (e) {
      toast.fromError(`lab ${name} failed to apply`, e);
    } finally {
      setBusy("");
    }
  };

  const groups = labs.reduce<Record<string, Lab[]>>((m, l) => {
    (m[l.group] ??= []).push(l);
    return m;
  }, {});

  const [snapName, setSnapName] = useState("");
  const [snapBusy, setSnapBusy] = useState("");
  const [restoreSnap, setRestoreSnap] = useState<string | null>(null);
  const [snapResult, setSnapResult] = useState<{ snapshot: string; restored: number; failed: number; nodes: { device: string; ok: boolean; diff_lines: number; error: string }[] } | null>(null);

  const takeSnap = async () => {
    if (!snapName.trim()) { toast.err("snapshot name required"); return; }
    setSnapBusy(snapName);
    try {
      const r = await api.takeSnapshot(snapName.trim());
      toast.ok(`snapshot ${r.name} taken`, `${r.devices} devices recorded`);
      setSnapName("");
      loadTickReload();
    } catch (e) { toast.fromError("snapshot failed", e); }
    finally { setSnapBusy(""); }
  };

  const doRestoreSnap = async () => {
    if (!restoreSnap) return;
    const name = restoreSnap;
    setRestoreSnap(null);
    setSnapBusy(name);
    setSnapResult(null);
    try {
      const r = await api.restoreSnapshot(name);
      setSnapResult(r);
      if (r.failed) toast.warn(`${name}: ${r.failed} device(s) failed`, "see per-device results");
      else toast.ok(`${name} restored`, `${r.restored} devices`);
    } catch (e) { toast.fromError(`restore of ${name} failed`, e); }
    finally { setSnapBusy(""); }
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_360px]">
      <div className="grid content-start gap-4">
        <section className="rounded-[0.25rem] border border-edge bg-card">
          <div className="flex items-center gap-2 border-b border-edge px-4 py-2">
            <span className="font-mono text-[10px] uppercase tracking-widest text-dim-neutral">
              lab snapshots — whole-lab named states · restore anytime
            </span>
            <input
              value={snapName} onChange={(e) => setSnapName(e.target.value)}
              placeholder="snapshot name (e.g. lab-mpls-1)" aria-label="snapshot name"
              className="ml-auto w-52 rounded-[0.25rem] border border-edge bg-black px-2 py-1 font-mono text-[11px] placeholder:text-dimmer-neutral"
            />
            <button onClick={takeSnap} disabled={snapBusy !== ""}
              className="rounded-[0.25rem] bg-ok/10 px-3 py-1 font-mono text-xs text-ok hover:bg-ok/20 disabled:opacity-40"
              aria-label="take snapshot">
              {snapBusy === snapName ? "working…" : "◉ snapshot lab now"}
            </button>
          </div>
          <div className="grid max-h-56 gap-0.5 overflow-y-auto p-2">
            {snapshots.map((s) => (
              <div key={s.name} className="flex items-center gap-3 rounded-[0.25rem] px-2 py-1 font-mono text-[11px] hover:bg-panel">
                <span className="text-accent-soft">{s.name}</span>
                <span className="text-dimmer-neutral">{s.at.slice(0, 19)}</span>
                <span className="text-dim-neutral">{s.devices} devices</span>
                <button
                  onClick={() => setRestoreSnap(s.name)}
                  disabled={snapBusy !== ""}
                  className="ml-auto rounded-[0.25rem] bg-warn/10 px-2 py-0.5 text-[10px] text-warn hover:bg-warn/20 disabled:opacity-40"
                  aria-label={`restore snapshot ${s.name}`}>
                  ↩ restore lab
                </button>
              </div>
            ))}
            {snapshots.length === 0 && <div className="p-2 text-[11px] text-dimmer-neutral">no snapshots yet — take one before making lab changes</div>}
          </div>
          {snapResult && (
            <div className="border-t border-edge p-2">
              <div className="mb-1 font-mono text-xs">
                <span className="text-accent-soft">{snapResult.snapshot}</span>
                <span className={snapResult.failed ? " text-err" : " text-ok"}> — {snapResult.restored} ok / {snapResult.failed} failed</span>
              </div>
              <div className="grid max-h-40 gap-0.5 overflow-y-auto font-mono text-[11px]">
                {snapResult.nodes.map((n) => (
                  <div key={n.device} className="flex gap-3">
                    <span className={n.ok ? "text-ok" : "text-err"}>{n.ok ? "✓" : "✗"}</span>
                    <span className="w-16 text-accent-soft">{n.device}</span>
                    <span className="text-dim-neutral">{n.ok ? `${n.diff_lines} lines changed` : n.error.slice(0, 70)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>
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
        {labs.length === 0 && <Skeleton lines={5} />}

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
      {restoreSnap && (
        <ConfirmModal
          title={`restore the whole lab to '${restoreSnap}'?`}
          body="Every device gets its recorded configuration pushed back with mode OVERRIDE (whole-config replace) via confirmed commit. Devices not in the snapshot are untouched."
          confirmLabel="restore lab"
          danger
          onConfirm={doRestoreSnap}
          onCancel={() => setRestoreSnap(null)}
        />
      )}
    </div>
  );
}
