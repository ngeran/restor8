import { useEffect, useState } from "react";
import { api, type Device, type Topology } from "./api";
import { useToast } from "./toast";
import { Retry, Skeleton, ConfirmModal } from "./ui";
import { useResource } from "./resource";

// §6: full inventory CRUD — add/edit/delete with forms (auth_ref stays a
// Secret NAME, never a credential), search/filter, and a detail drawer
// with quick actions. Read path shares the useResource cache with every
// other screen.

const EMPTY: Partial<Device> = { name: "", mgmt_ip: "", port: 830, platform: "CRPD", auth_ref: "lab-auth-root", containerlab_node: "" };

export default function Devices({ onSelectDevice }: { onSelectDevice?: (name: string) => void }) {
  const devicesQ = useResource("devices", api.devices);
  const topoQ = useResource("topology", api.topology);
  const devices = devicesQ.data ?? [];
  const topo = topoQ.data ?? null;
  const toast = useToast();

  const [q, setQ] = useState("");
  const [form, setForm] = useState<Partial<Device> | null>(null);   // add/edit
  const [deleting, setDeleting] = useState<Device | null>(null);
  const [drawer, setDrawer] = useState<Device | null>(null);

  useEffect(() => {
    if (devicesQ.error) toast.fromError("devices failed to load", devicesQ.error);
  }, [devicesQ.error]);

  const role = (d: Device) => topo?.nodes.find((n) => n.name === d.name)?.role ?? "—";
  const shown = devices.filter(
    (d) => !q || d.name.toLowerCase().includes(q.toLowerCase()) || role(d).toLowerCase() === q.toLowerCase() || d.platform.toLowerCase().includes(q.toLowerCase()),
  );

  const save = async () => {
    if (!form?.name || !form?.mgmt_ip) { toast.err("name and mgmt address are required"); return; }
    try {
      if (form.id) {
        await api.updateDevice(form.id, form);
        toast.ok(`${form.name} updated`);
      } else {
        await api.createDevice(form as Record<string, unknown>);
        toast.ok(`${form.name} registered`);
      }
      setForm(null);
      devicesQ.reload();
    } catch (e) { toast.fromError("save failed", e); }
  };

  const del = async () => {
    if (!deleting) return;
    try {
      await api.deleteDevice(deleting.id);
      toast.ok(`${deleting.name} removed from inventory`);
      setDeleting(null);
      devicesQ.reload();
    } catch (e) { toast.fromError("delete failed", e); }
  };

  const F = (k: keyof Device, label: string, type = "text") => (
    <label key={k} className="grid gap-1">
      <span className="font-mono text-[10px] uppercase tracking-widest text-dim-neutral">{label}</span>
      <input
        type={type}
        value={String(form?.[k] ?? "")}
        onChange={(e) => setForm((f) => ({ ...f!, [k]: type === "number" ? Number(e.target.value) : e.target.value }))}
        className="rounded-[0.25rem] border border-edge bg-black px-2 py-1.5 font-mono text-xs"
      />
    </label>
  );

  return (
    <>
      <section className="rounded-[0.25rem] border border-edge bg-card">
        <div className="flex items-center gap-2 border-b border-edge px-4 py-3">
          <span className="font-mono text-xs uppercase tracking-widest text-dim-neutral">
            inventory — {shown.length}/{devices.length}
          </span>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="filter: name, role, platform…"
            aria-label="filter devices"
            className="ml-auto w-56 rounded-[0.25rem] border border-edge bg-black px-2 py-1 font-mono text-xs placeholder:text-dimmer-neutral"
          />
          <button onClick={() => setForm({ ...EMPTY })} className="rounded-[0.25rem] bg-accent/10 px-3 py-1 font-mono text-xs text-accent hover:bg-accent/20" aria-label="add device">
            + add
          </button>
        </div>
        {devicesQ.error && <Retry onRetry={() => devicesQ.reload()} note="devices failed to load" />}
        {!devicesQ.error && devices.length === 0 && <Skeleton lines={6} />}
        <div className="overflow-x-auto">
          <table className="w-full font-mono text-xs">
            <thead>
              <tr className="border-b border-edge text-left text-[10px] uppercase tracking-wider text-dim-neutral">
                {["name", "role", "platform", "mgmt address", "auth", "clab node", ""].map((h) => (
                  <th key={h} className="px-4 py-2">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map((d) => (
                <tr key={d.id} className="cursor-pointer border-b border-edge/50 hover:bg-panel" onClick={() => setDrawer(d)}>
                  <td className="px-4 py-2 text-accent-soft">{d.name}</td>
                  <td className="px-4 py-2 text-secondary">{role(d)}</td>
                  <td className="px-4 py-2">{d.platform}</td>
                  <td className="px-4 py-2 text-dim-neutral">{d.mgmt_ip}:{d.port}</td>
                  <td className="px-4 py-2 text-dim-neutral">{d.auth_ref}</td>
                  <td className="px-4 py-2 text-dim-neutral">{d.containerlab_node ?? "—"}</td>
                  <td className="px-4 py-2 text-right whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
                    <button onClick={() => setForm(d)} aria-label={`edit ${d.name}`} className="mr-2 text-dim-neutral hover:text-accent">✎</button>
                    <button onClick={() => setDeleting(d)} aria-label={`delete ${d.name}`} className="text-dim-neutral hover:text-err">✕</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {form && (
        <div className="fixed inset-0 z-40 grid place-items-center bg-black/80 p-4" role="dialog" aria-modal="true" onClick={() => setForm(null)}>
          <div className="w-full max-w-md rounded-[0.25rem] border border-edge bg-card p-4" onClick={(e) => e.stopPropagation()}>
            <div className="mb-3 font-mono text-sm text-accent-soft">{form.id ? `edit ${form.name}` : "register device"}</div>
            <div className="grid gap-3">
              {F("name", "name")}
              {F("mgmt_ip", "mgmt address (cluster-reachable)")}
              {F("port", "NETCONF port", "number")}
              {F("platform", "platform")}
              {F("auth_ref", "auth_ref (k8s Secret NAME — never a password here)")}
              {F("containerlab_node", "containerlab/clabernetes node")}
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <button onClick={() => setForm(null)} className="rounded-[0.25rem] border border-edge px-3 py-1 font-mono text-xs text-dim-neutral">cancel</button>
              <button onClick={save} className="rounded-[0.25rem] bg-accent/10 px-3 py-1 font-mono text-xs text-accent hover:bg-accent/20">save</button>
            </div>
          </div>
        </div>
      )}

      {deleting && (
        <ConfirmModal
          title={`remove ${deleting.name} from inventory?`}
          body="Backups and run history stay in Git/SQLite; only the registry entry goes. Re-register with the same name to relink."
          confirmLabel="remove"
          danger
          onConfirm={del}
          onCancel={() => setDeleting(null)}
        />
      )}

      {drawer && (
        <div className="fixed inset-0 z-30 flex justify-end bg-black/60" onClick={() => setDrawer(null)}>
          <aside className="h-full w-80 overflow-y-auto border-l border-edge bg-card p-4" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={`${drawer.name} details`}>
            <div className="mb-3 flex items-center justify-between">
              <span className="font-mono text-sm text-accent-soft">{drawer.name}</span>
              <button onClick={() => setDrawer(null)} aria-label="close" className="font-mono text-xs text-dim-neutral">esc ✕</button>
            </div>
            <div className="grid gap-1 font-mono text-[11px] text-dim-neutral">
              <span>role: <span className="text-secondary">{role(drawer)}</span></span>
              <span>mgmt: {drawer.mgmt_ip}:{drawer.port}</span>
              <span>platform: {drawer.platform}</span>
              <span>auth_ref: {drawer.auth_ref}</span>
              <span>registered: {drawer.created_at.slice(0, 19)}</span>
            </div>
            <div className="mt-4 grid gap-1">
              <button
                onClick={async () => {
                  try { const r = await api.backupNow(drawer.id); toast.ok(`backup: ${r.commit ?? "no change"}`); }
                  catch (e) { toast.fromError("backup failed", e); }
                }}
                className="rounded-[0.25rem] bg-ok/10 px-3 py-1 font-mono text-xs text-ok hover:bg-ok/20"
                aria-label="backup now"
              >⬇ backup now</button>
              <button
                onClick={() => { onSelectDevice?.(drawer.name); setDrawer(null); }}
                className="rounded-[0.25rem] bg-accent/10 px-3 py-1 font-mono text-xs text-accent hover:bg-accent/20"
              >→ configurations</button>
            </div>
          </aside>
        </div>
      )}
    </>
  );
}
