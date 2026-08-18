import { useEffect, useState } from "react";
import { api, type BackupEntry, type Device, type Template } from "./api";
import { useToast } from "./toast";
import { ConfirmModal, Skeleton } from "./ui";
import { useResource } from "./resource";
import Stepper from "./Stepper";

// The flagship: read the running config, edit via grouped template forms
// (or raw set-format), preview the payload, push (merge or override),
// and walk Git history with diffs + one-click gated restore.


export function classifyDiffLine(line: string): "header" | "add" | "del" | "ctx" {
  // MUST be separate startsWith calls — a parenthesized comma expression
  // ("+++", "---", "@@") evaluates to only "@@" (the shipped bug).
  if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("@@")) return "header";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "ctx";
}

type Tab = "running" | "editor" | "history";

export default function Configurations({ initialDevice }: { initialDevice?: string | null }) {
  const [selected, setSelected] = useState<Device | null>(null);
  const [tab, setTab] = useState<Tab>("running");

  const devicesQ = useResource("devices", api.devices);
  const devices = devicesQ.data ?? [];
  useEffect(() => {
    if (!devices.length) return;
    const want = initialDevice ? devices.find((d) => d.name === initialDevice) : undefined;
    if (!selected || (initialDevice && selected.name !== initialDevice)) setSelected(want ?? devices[0]);
  }, [devices, initialDevice]);

  const [refreshKey, setRefreshKey] = useState(0);
  const toast = useToast();

  return (
    <div className="grid gap-4 lg:grid-cols-[240px_1fr]">
      <aside className="grid content-start gap-4">
        <div className="rounded-[0.25rem] border border-edge bg-card">
          <div className="flex items-center justify-between border-b border-edge px-3 py-2">
            <span className="font-mono text-[10px] uppercase tracking-widest text-dim-neutral">device</span>
            {selected && (
              <button
                onClick={async () => {
                  try {
                    const r = await api.backupNow(selected.id);
                    toast.ok(`backup: ${r.commit ?? "no change"}`, r.changed ? "new commit in history" : "config unchanged — no commit");
                    setRefreshKey((k) => k + 1);
                  } catch (e) { toast.fromError(`backup of ${selected.name} failed`, e); }
                }}
                className="font-mono text-[10px] text-ok hover:glow-ok"
                title="backup running config → git"
              >
                ⬇ backup
              </button>
            )}
          </div>
          <div className="grid gap-0.5 p-2">
            {devices.map((d) => (
              <button
                key={d.id}
                onClick={() => setSelected(d)}
                className={`rounded-[0.25rem] px-2 py-1 text-left font-mono text-xs ${
                  selected?.id === d.id ? "bg-accent/10 text-accent" : "text-dim-neutral hover:text-accent-soft"
                }`}
              >
                {d.name}
              </button>
            ))}
          </div>
        </div>
        <div className="rounded-[0.25rem] border border-edge bg-card p-3 font-mono text-[10px] leading-5 text-dimmer-neutral">
          pushes ride<br />lock → load → diff<br /><span className="text-warn">commit confirmed</span><br />events stream live
        </div>
      </aside>

      {selected && (
        <section className="rounded-[0.25rem] border border-edge bg-card">
          <div className="flex items-center gap-1 border-b border-edge px-3 py-2">
            <span className="mr-3 font-mono text-xs text-accent-soft">{selected.name}</span>
            {(["running", "editor", "history"] as Tab[]).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`rounded-[0.25rem] px-3 py-1 font-mono text-[11px] uppercase tracking-wider ${
                  tab === t ? "bg-accent/10 text-accent" : "text-dim-neutral hover:text-accent-soft"
                }`}
              >
                {t}
              </button>
            ))}
          </div>
          {tab === "running" && <Running device={selected} refreshKey={refreshKey} />}
          {tab === "editor" && <Editor device={selected} onPushed={() => setRefreshKey((k) => k + 1)} />}
          {tab === "history" && <History device={selected} refreshKey={refreshKey} />}
        </section>
      )}
    </div>
  );
}

function Running({ device, refreshKey }: { device: Device; refreshKey: number }) {
  const [config, setConfig] = useState("");
  const [fmt, setFmt] = useState<"set" | "text">("set");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    // request-id guard: a fast device switch must not let a stale reply
    // overwrite the new device's config
    let live = true;
    setLoading(true);
    setError("");
    api.running(device.id, fmt)
      .then((r) => { if (live) setConfig(r.config); })
      .catch((e) => { if (live) setError(String(e)); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [device.id, fmt, refreshKey]);

  return (
    <div>
      <div className="flex items-center gap-2 border-b border-edge px-4 py-2">
        {(["set", "text"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFmt(f)}
            className={`rounded-[0.25rem] px-2 py-0.5 font-mono text-[11px] ${fmt === f ? "text-accent" : "text-dim-neutral"}`}
          >
            {f}
          </button>
        ))}
        <span className="ml-auto font-mono text-[10px] text-dimmer-neutral">{config.split("\n").length} lines · live from device</span>
      </div>
      {error && <div className="p-4 font-mono text-xs text-err">{error}</div>}
      {loading && <Skeleton lines={8} />}
      {!loading && !error && (
        <pre className="max-h-[70vh] overflow-auto px-4 py-3 font-mono text-[11px] leading-5 whitespace-pre-wrap break-all">{config}</pre>
      )}
    </div>
  );
}

function Editor({ device, onPushed }: { device: Device; onPushed: () => void }) {
  const toast = useToast();
  const [templates, setTemplates] = useState<Template[]>([]);
  const [tplName, setTplName] = useState<string>("__raw");
  const [values, setValues] = useState<Record<string, string>>({});
  const [payload, setPayload] = useState("");
  const [mode, setMode] = useState<"merge" | "override">("merge");
  const [result, setResult] = useState<{ ok: boolean; text: string; diff: string } | null>(null);
  const [busy, setBusy] = useState(false);
  // §3: two-phase confirmed-commit — default ON. A push parks in the
  // device's 5-minute window; the human confirms or rolls back below.
  const [twoPhase, setTwoPhase] = useState(true);
  const [pending, setPending] = useState<{ id: string; secs: number } | null>(null);
  const [lastSessionId, setLastSessionId] = useState<string>("");
  const [needOverrideModal, setNeedOverrideModal] = useState(false);

  useEffect(() => {
    api.templates().then(setTemplates).catch(() => {});
  }, []);

  const tpl = templates.find((t) => t.name === tplName);

  // authoritative countdown from connector's held-session clock
  useEffect(() => {
    if (!pending) return;
    const t = window.setInterval(() => {
      api.sessionStatus(pending.id)
        .then((s) => setPending((p) => (p ? { ...p, secs: Math.round(s.expires_in) } : null)))
        .catch(() => setPending(null)); // window lapsed → device self-reverted
    }, 3000);
    return () => clearInterval(t);
  }, [pending?.id]);

  useEffect(() => {
    // defaults from the schema whenever the template changes
    const d: Record<string, string> = {};
    tpl?.fields.forEach((f) => {
      if (f.default !== undefined) d[f.name] = String(f.default);
    });
    setValues(d);
    setPayload("");
    setResult(null);
  }, [tplName]);

  const groups = templates.reduce<Record<string, Template[]>>((m, t) => {
    (m[t.group] ??= []).push(t);
    return m;
  }, {});

  const preview = async () => {
    setResult(null);
    if (tplName === "__raw") return;
    try {
      const r = await api.render(tplName, values);
      setPayload(r.payload);
      setMode(r.mode === "override" ? "override" : "merge");
    } catch (e) {
      setResult({ ok: false, text: String(e), diff: "" });
    }
  };

  const doPush = async () => {
    setBusy(true);
    setResult(null);
    try {
      const r = await api.push({
        device_id: device.id,
        payload,
        mode,
        fmt: "set",
        comment: `restor8-ui editor (${tplName === "__raw" ? "raw" : tplName})`,
        confirm_now: !twoPhase,
      });
      setLastSessionId(r.session_id);
      if (r.confirmed) {
        setResult({ ok: true, text: "committed", diff: r.diff });
        toast.ok(`push to ${device.name}: committed`);
      } else {
        setPending({ id: r.session_id, secs: 300 });
        setResult({ ok: true, text: "awaiting confirmation", diff: r.diff });
        toast.warn(`push to ${device.name}: in the confirmed-commit window`, "confirm or roll back below");
      }
      onPushed();
    } catch (e) {
      setResult({ ok: false, text: "", diff: "" });
      toast.fromError(`push to ${device.name} failed`, e);
    } finally {
      setBusy(false);
    }
  };

  const push = () => {
    if (mode === "override") { setNeedOverrideModal(true); return; }
    doPush();
  };

  const confirmNow = async () => {
    if (!pending) return;
    try {
      await api.sessionConfirm(pending.id);
      toast.ok(`${device.name}: commit confirmed`, "candidate is now permanent");
      setPending(null);
      setResult(null);
      onPushed();
    } catch (e) {
      toast.fromError("confirm failed", e);
    }
  };

  const rollbackNow = async () => {
    if (!pending) return;
    try {
      const r = await api.sessionRollback(pending.id);
      toast.warn(`${device.name}: rolled back`, "device returned to previous config");
      setPending(null);
      setResult({ ok: true, text: "rolled back — device at previous config", diff: r.diff ?? "" });
    } catch (e) {
      toast.fromError("rollback failed", e);
    }
  };

  return (
    <div className="grid gap-4 p-4 lg:grid-cols-[300px_1fr]">
      <div className="grid content-start gap-3">
        <label className="grid gap-1">
          <span className="font-mono text-[10px] uppercase tracking-widest text-dim-neutral">template</span>
          <select
            value={tplName}
            onChange={(e) => setTplName(e.target.value)}
            className="rounded-[0.25rem] border border-edge bg-black px-2 py-1.5 font-mono text-xs text-accent-soft"
          >
            <option value="__raw">— raw payload —</option>
            {Object.entries(groups).map(([g, items]) => (
              <optgroup key={g} label={g}>
                {items.map((t) => (
                  <option key={t.name} value={t.name}>{t.name} — {t.description}</option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>

        {tpl ? (
          tpl.fields.map((f) => (
            <label key={f.name} className="grid gap-1">
              <span className="font-mono text-[10px] uppercase tracking-widest text-dim-neutral">{f.label}</span>
              {f.type === "select" ? (
                <select
                  value={values[f.name] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
                  className="rounded-[0.25rem] border border-edge bg-black px-2 py-1.5 font-mono text-xs"
                >
                  {(f.options ?? []).map((o) => <option key={o}>{o}</option>)}
                </select>
              ) : f.type === "bool" ? (
                <button
                  onClick={() => setValues((v) => ({ ...v, [f.name]: v[f.name] === "false" ? "true" : "false" }))}
                  className={`rounded-[0.25rem] border border-edge px-2 py-1.5 text-left font-mono text-xs ${values[f.name] === "false" ? "text-dimmer-neutral" : "text-ok"}`}
                >
                  {values[f.name] === "false" ? "false" : "true"}
                </button>
              ) : (
                <input
                  type={f.type === "number" ? "number" : "text"}
                  value={values[f.name] ?? ""}
                  placeholder={f.placeholder ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
                  className="rounded-[0.25rem] border border-edge bg-black px-2 py-1.5 font-mono text-xs placeholder:text-dimmer-neutral"
                />
              )}
            </label>
          ))
        ) : (
          <div className="rounded-[0.25rem] border border-edge p-2 font-mono text-[10px] leading-4 text-dimmer-neutral">
            raw set-format lines, e.g.<br />set interfaces eth2 description "x"<br />delete protocols bgp group OLD
          </div>
        )}

        {tpl && (
          <button onClick={preview} className="rounded-[0.25rem] bg-secondary/10 px-3 py-1.5 font-mono text-xs text-secondary hover:bg-secondary/20">
            ⚙ render preview
          </button>
        )}
      </div>

      <div className="grid content-start gap-3">
        <div className="grid gap-1">
          <div className="flex items-center justify-between">
            <span className="font-mono text-[10px] uppercase tracking-widest text-dim-neutral">
              payload (editable)
            </span>
            <div className="flex items-center gap-1">
              <button
                onClick={() => setTwoPhase((v) => !v)}
                title="confirmed-commit window: push lands, you confirm or roll back within 5 minutes"
                className={`mr-2 rounded-[0.25rem] px-2 py-0.5 font-mono text-[11px] ${twoPhase ? "text-warn" : "text-dimmer-neutral"}`}
                aria-label="toggle confirmed-commit window"
              >
                {twoPhase ? "▣ 2-phase" : "□ 2-phase"}
              </button>
              {(["merge", "override"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={`rounded-[0.25rem] px-2 py-0.5 font-mono text-[11px] ${
                    mode === m ? (m === "override" ? "text-err" : "text-ok") : "text-dimmer-neutral"
                  }`}
                  title={m === "override" ? "REPLACES the whole configuration" : "adds to existing config"}
                >
                  {m}
                </button>
              ))}
            </div>
          </div>
          <textarea
            value={payload}
            onChange={(e) => setPayload(e.target.value)}
            rows={12}
            spellCheck={false}
            placeholder="render a template above, or write set-format lines here"
            className="rounded-[0.25rem] border border-edge bg-black px-3 py-2 font-mono text-[11px] leading-5 text-text placeholder:text-dimmer-neutral focus:border-accent"
          />
        </div>

        <div className="flex items-center gap-3">
          <button
            onClick={push}
            disabled={busy || !payload.trim()}
            className="rounded-[0.25rem] bg-accent/10 px-4 py-1.5 font-mono text-xs text-accent hover:bg-accent/20 disabled:opacity-40"
          >
            {busy ? "pushing…" : `▶ push to ${device.name}`}
          </button>
          {mode === "override" && <span className="font-mono text-[10px] text-err">override replaces the ENTIRE config</span>}
        </div>

        {pending && (
          <div className="rounded-[0.25rem] border border-warn/50 bg-warn/5 p-3">
            <div className="mb-2 flex items-center justify-between font-mono text-xs">
              <span className="text-warn">
                ◌ confirmed-commit window — {device.name} auto-reverts in{" "}
                {String(Math.floor(pending.secs / 60)).padStart(2, "0")}:{String(pending.secs % 60).padStart(2, "0")}
              </span>
              <span className="text-dimmer-neutral">session {pending.id.slice(0, 8)}</span>
            </div>
            <div className="flex gap-2">
              <button onClick={confirmNow} className="rounded-[0.25rem] bg-ok/10 px-3 py-1 font-mono text-xs text-ok hover:bg-ok/20">
                ✓ confirm commit
              </button>
              <button onClick={rollbackNow} className="rounded-[0.25rem] bg-err/10 px-3 py-1 font-mono text-xs text-err hover:bg-err/20">
                ↩ rollback
              </button>
            </div>
          </div>
        )}
        {needOverrideModal && (
          <ConfirmModal
            title="override replaces the ENTIRE configuration"
            body={`You are about to REPLACE ${device.name}'s whole running config with the ${payload.split("\n").length} lines in the payload. Everything not present in it — interfaces, BGP, management — is removed. This is the nuclear option.`}
            confirmLabel="push override"
            danger
            onConfirm={() => { setNeedOverrideModal(false); doPush(); }}
            onCancel={() => setNeedOverrideModal(false)}
          />
        )}
        {lastSessionId && (
          <div className="rounded-[0.25rem] border border-edge bg-black p-2">
            <Stepper sessionId={lastSessionId} />
          </div>
        )}
        {result && (
          <div className={`rounded-[0.25rem] border p-3 ${result.ok ? "border-ok/40" : "border-err/40"}`}>
            <div className={`mb-1 font-mono text-xs ${result.ok ? "text-ok" : "text-err"}`}>
              {result.ok ? "✓ " : "✗ "}{result.text}
            </div>
            {result.diff && <DiffView diff={result.diff} />}
          </div>
        )}
      </div>
    </div>
  );
}

function History({ device, refreshKey }: { device: Device; refreshKey: number }) {
  const toast = useToast();
  const [history, setHistory] = useState<BackupEntry[]>([]);
  const [sha, setSha] = useState("");
  const [diff, setDiff] = useState<{ changed_lines?: number; diff?: string } | null>(null);
  const [restoring, setRestoring] = useState(false);
  const [restoreMsg, setRestoreMsg] = useState("");
  const [needRestoreModal, setNeedRestoreModal] = useState(false);

  useEffect(() => {
    let live = true;
    setSha("");
    setDiff(null);
    setRestoreMsg("");
    api.backups(device.id).then((h) => { if (live) setHistory(h); }).catch(() => {});
    return () => { live = false; };
  }, [device.id, refreshKey]);

  useEffect(() => {
    let live = true;
    if (!sha) return;
    setDiff(null);
    api.diff(device.id, sha)
      .then((d) => { if (live) setDiff(d); })
      .catch(() => { if (live) setDiff({}); });
    return () => { live = false; };
  }, [device.id, sha]);

  const restore = async () => {
    setNeedRestoreModal(false);
    setRestoring(true);
    setRestoreMsg("");
    try {
      const r = await api.restore(device.id, sha) as Record<string, unknown>;
      const v = r.validation as Record<string, unknown> | undefined;
      setRestoreMsg(
        r.restored ? `restored ✓ (${v?.check}: passed)` : `rolled back — validation: ${JSON.stringify(v?.check)}`,
      );
      if (r.restored) toast.ok(`${device.name} restored`, `validation: ${v?.check} passed`);
      else toast.warn(`${device.name} rolled back`, "post-restore validation failed — device returned to known-good");
    } catch (e) {
      setRestoreMsg("");
      toast.fromError(`restore of ${device.name} failed`, e);
    } finally {
      setRestoring(false);
    }
  };

  return (
    <div className="grid gap-4 p-4 lg:grid-cols-[240px_1fr]">
      <div className="grid content-start gap-1">
        {history.map((h) => (
          <button
            key={h.sha}
            onClick={() => setSha(h.sha)}
            className={`rounded-[0.25rem] px-2 py-1 text-left font-mono text-[11px] ${
              sha === h.sha ? "bg-accent/10 text-accent" : "text-dim-neutral hover:text-accent-soft"
            }`}
          >
            <div className="text-secondary">{h.sha}</div>
            <div className="text-[10px] text-dimmer-neutral">{h.date.slice(0, 19)}</div>
          </button>
        ))}
        {history.length === 0 && <div className="font-mono text-[11px] text-dimmer-neutral">no backups</div>}
      </div>
      <div>
        {sha && (
          <>
            <div className="mb-2 flex items-center gap-3">
              <span className={`font-mono text-xs ${diff?.changed_lines ? "text-warn" : "text-ok"}`}>
                {diff?.changed_lines ? `${diff.changed_lines} changed lines` : diff ? "in sync" : "…"}
              </span>
              <button
                onClick={() => setNeedRestoreModal(true)}
                disabled={restoring}
                className="rounded-[0.25rem] bg-warn/10 px-3 py-1 font-mono text-xs text-warn hover:bg-warn/20 disabled:opacity-40"
                title="push this backup back through the confirmed-commit + validation gate"
              >
                {restoring ? "restoring…" : "↩ restore to this commit"}
              </button>
              {restoreMsg && <span className="font-mono text-[11px] text-accent-soft">{restoreMsg}</span>}
            </div>
            {diff?.diff !== undefined && <DiffView diff={diff.diff ?? ""} />}
            {needRestoreModal && (
              <ConfirmModal
                title={`restore ${device.name} to ${sha}`}
                body="The backup is pushed back with mode OVERRIDE (whole-config replace) inside a confirmed-commit window. Post-restore validation (config-match) runs automatically; a failed check rolls the device back to its current state."
                confirmLabel="restore with override"
                danger
                onConfirm={restore}
                onCancel={() => setNeedRestoreModal(false)}
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}

function DiffView({ diff }: { diff: string }) {
  const lines = diff ? diff.split("\n") : ["(no differences)"];
  let n = 0;
  return (
    <div className="max-h-[46vh] overflow-auto rounded-[0.25rem] bg-black font-mono text-[11px] leading-5">
      {lines.map((line, i) => {
        const kind = classifyDiffLine(line);
        const cls = kind === "header" ? "text-secondary"
          : kind === "add" ? "text-ok bg-ok/10" : kind === "del" ? "text-err bg-err/10" : "text-dim-neutral";
        const countable = classifyDiffLine(line) !== "header";
        if (countable) n += 1;
        return (
          <div key={i} className={`flex ${cls}`}>
            <span className="w-10 shrink-0 select-none pr-2 text-right text-dimmer-neutral">{countable ? n : ""}</span>
            <pre className="whitespace-pre-wrap break-all">{line || " "}</pre>
          </div>
        );
      })}
    </div>
  );
}
