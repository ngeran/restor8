import { useEffect, useState } from "react";
import { api, type Device, type Run, type Topology } from "./api";
import { useEvents } from "./events";

const statusColor = (s: string) =>
  s === "passed" ? "text-ok" : s === "failed" ? "text-err" : "text-warn";

export default function Dashboard({ onGoto }: { onGoto: (t: string) => void }) {
  const [devices, setDevices] = useState<Device[]>([]);
  const [topo, setTopo] = useState<Topology | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [busy, setBusy] = useState(false);
  const { events, state } = useEvents();

  const refresh = () => {
    api.devices().then(setDevices).catch(() => {});
    api.topology().then(setTopo).catch(() => {});
    api.runs().then(setRuns).catch(() => {});
  };
  useEffect(refresh, []);
  useEffect(() => {
    const t = setInterval(refresh, 10000);
    return () => clearInterval(t);
  }, []);

  const run = async () => {
    setBusy(true);
    try {
      await api.startRun("bgp-fabric");
    } finally {
      setBusy(false);
    }
  };

  const roles = topo?.nodes.reduce<Record<string, number>>((m, n) => {
    m[n.role] = (m[n.role] ?? 0) + 1;
    return m;
  }, {}) ?? {};

  return (
    <div className="grid gap-4">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Card label="devices" value={String(devices.length)} />
        <Card label="fabric links" value={String(topo?.links.length ?? 0)} />
        <Card label="underlay" value={topo?.underlay ?? "—"} small />
        <Card
          label="live feed"
          value={live ? "connected" : "offline"}
          valueClass={live ? "text-ok" : "text-err"}
          small
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="rounded-[0.25rem] border border-edge bg-card p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="font-mono text-xs uppercase tracking-widest text-[dim-neutral]">
              scenario runs
            </h2>
            <button
              onClick={run}
              disabled={busy}
              className="rounded-[0.25rem] bg-accent/10 px-3 py-1 font-mono text-xs text-accent hover:bg-accent/20 disabled:opacity-50"
            >
              {busy ? "starting…" : "▶ run bgp-fabric"}
            </button>
          </div>
          <div className="grid gap-1">
            {runs.slice(0, 8).map((r) => (
              <button
                key={r.id}
                onClick={() => onGoto("labs")}
                className="flex items-center justify-between rounded-[0.25rem] px-2 py-1 font-mono text-xs hover:bg-panel"
              >
                <span className="text-[dim-neutral]">#{r.id} {r.scenario}</span>
                <span className={statusColor(r.status)}>{r.status}</span>
              </button>
            ))}
            {runs.length === 0 && <Empty />}
          </div>
          <div className="mt-3 flex flex-wrap gap-2 font-mono text-[10px] text-[dim-neutral]">
            {Object.entries(roles).map(([r, n]) => (
              <span key={r} className="rounded-[0.25rem] border border-edge px-2 py-0.5">
                {r}×{n}
              </span>
            ))}
          </div>
        </section>

        <section className="rounded-[0.25rem] border border-edge bg-card p-4">
          <h2 className="mb-3 font-mono text-xs uppercase tracking-widest text-[dim-neutral]">
            live events {state.kind === "live" ? <span className="text-ok">●</span> : <span className="text-warn">◌ {state.inSecs}s</span>}
          </h2>
          <div className="grid max-h-72 gap-0.5 overflow-y-auto font-mono text-[11px]">
            {[...events].reverse().slice(0, 40).map((e, i) => (
              <div key={i} className="flex gap-2">
                <span className="shrink-0 text-[dimmer-neutral]">
                  {new Date((e.ts ?? 0) * 1000).toLocaleTimeString()}
                </span>
                <span className="w-28 shrink-0 truncate text-secondary">
                  {e.device ?? `run#${e.run ?? "?"}`}
                </span>
                <span className="text-accent-soft">{e.stage ?? e.phase ?? ""}</span>
                <span className="truncate text-[dim-neutral]">{e.message?.slice(0, 60)}</span>
              </div>
            ))}
            {events.length === 0 && <Empty text="waiting for activity…" />}
          </div>
        </section>
      </div>
    </div>
  );
}

function Card({
  label, value, valueClass = "text-accent", small = false,
}: { label: string; value: string; valueClass?: string; small?: boolean }) {
  return (
    <div className="rounded-[0.25rem] border border-edge bg-card p-4">
      <div className="font-mono text-[10px] uppercase tracking-widest text-[dim-neutral]">{label}</div>
      <div className={`mt-1 font-mono ${small ? "text-sm" : "text-2xl"} ${valueClass}`}>{value}</div>
    </div>
  );
}

function Empty({ text = "no data" }: { text?: string }) {
  return <div className="py-4 text-center font-mono text-xs text-[dimmer-neutral]">{text}</div>;
}
