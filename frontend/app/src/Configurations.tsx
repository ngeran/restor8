import { useEffect, useState } from "react";
import { api, type BackupEntry, type Device } from "./api";

// The flagship screen (spec §3/§7): git history per device + the unified
// diff of running config vs the selected backup — rendered exactly like
// the mockup's red/green blocks with line numbers.

interface DiffPayload {
  device?: string;
  sha?: string;
  changed_lines?: number;
  diff?: string;
}

export default function Configurations() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [selected, setSelected] = useState<Device | null>(null);
  const [history, setHistory] = useState<BackupEntry[]>([]);
  const [sha, setSha] = useState<string>("");
  const [diff, setDiff] = useState<DiffPayload | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.devices().then((ds) => {
      setDevices(ds);
      if (ds.length && !selected) setSelected(ds[0]);
    }).catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    if (!selected) return;
    setSha("");
    setDiff(null);
    api.backups(selected.id).then(setHistory).catch((e) => setError(String(e)));
  }, [selected]);

  useEffect(() => {
    if (!selected || !sha) return;
    setDiff(null);
    setError("");
    api.diff(selected.id, sha).then(setDiff).catch((e) => setError(String(e)));
  }, [selected, sha]);

  return (
    <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
      <section className="grid content-start gap-4">
        <div className="rounded-[0.25rem] border border-edge bg-card">
          <div className="border-b border-edge px-3 py-2 font-mono text-[10px] uppercase tracking-widest text-[#8b97b8]">
            device
          </div>
          <div className="grid gap-0.5 p-2">
            {devices.map((d) => (
              <button
                key={d.id}
                onClick={() => setSelected(d)}
                className={`rounded-[0.25rem] px-2 py-1 text-left font-mono text-xs ${
                  selected?.id === d.id
                    ? "bg-accent/10 text-accent glow-accent"
                    : "text-[#8b97b8] hover:text-accent-soft"
                }`}
              >
                {d.name}
              </button>
            ))}
          </div>
        </div>

        <div className="rounded-[0.25rem] border border-edge bg-card">
          <div className="border-b border-edge px-3 py-2 font-mono text-[10px] uppercase tracking-widest text-[#8b97b8]">
            commits — {history.length}
          </div>
          <div className="grid max-h-96 gap-0.5 overflow-y-auto p-2">
            {history.map((h) => (
              <button
                key={h.sha}
                onClick={() => setSha(h.sha)}
                className={`rounded-[0.25rem] px-2 py-1 text-left font-mono text-[11px] ${
                  sha === h.sha ? "bg-accent/10 text-accent" : "text-[#8b97b8] hover:text-accent-soft"
                }`}
              >
                <div className="text-secondary">{h.sha}</div>
                <div className="text-[10px] text-[#5b6785]">{h.date.slice(0, 19)}</div>
              </button>
            ))}
            {history.length === 0 && (
              <div className="p-2 font-mono text-[11px] text-[#5b6785]">no backups yet</div>
            )}
          </div>
        </div>
      </section>

      <section className="rounded-[0.25rem] border border-edge bg-card">
        <div className="flex items-center justify-between border-b border-edge px-4 py-3">
          <span className="font-mono text-xs uppercase tracking-widest text-[#8b97b8]">
            {selected ? `${selected.name} — running vs backup` : "diff"}
          </span>
          {diff && (
            <span className={`font-mono text-xs ${diff.changed_lines ? "text-warn" : "text-ok"}`}>
              {diff.changed_lines ? `${diff.changed_lines} changed lines` : "in sync"}
            </span>
          )}
        </div>
        {error && <div className="p-4 font-mono text-xs text-err">{error}</div>}
        {!selected && (
          <div className="p-4 font-mono text-xs text-[#5b6785]">select a device</div>
        )}
        {selected && !sha && !error && (
          <div className="p-4 font-mono text-xs text-[#5b6785]">
            select a commit to diff it against the running config
          </div>
        )}
        {diff && <DiffView diff={diff.diff ?? ""} />}
      </section>
    </div>
  );
}

function DiffView({ diff }: { diff: string }) {
  const lines = diff ? diff.split("\n") : ["(no differences — running config matches this backup)"];
  let n = 0;
  return (
    <div className="max-h-[70vh] overflow-auto font-mono text-[11px] leading-5">
      {lines.map((line, i) => {
        const cls = line.startsWith("+++") || line.startsWith("---") || line.startsWith("@@")
          ? "text-secondary bg-secondary/5"
          : line.startsWith("+")
            ? "text-ok bg-ok/10"
            : line.startsWith("-")
              ? "text-err bg-err/10"
              : "text-[#8b97b8]";
        const countable = !line.startsWith(("+++", "---", "@@"));
        if (countable) n += 1;
        return (
          <div key={i} className={`flex ${cls}`}>
            <span className="w-10 shrink-0 select-none pr-2 text-right text-[#5b6785]">
              {countable ? n : ""}
            </span>
            <pre className="whitespace-pre-wrap break-all">{line || " "}</pre>
          </div>
        );
      })}
    </div>
  );
}
