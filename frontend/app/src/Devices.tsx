import { useEffect, useState } from "react";
import { api, type Device, type Topology } from "./api";
import { useToast } from "./toast";
import { Retry, Skeleton } from "./ui";
import { useResource } from "./resource";

export default function Devices() {
  const toast = useToast();

  const devicesQ = useResource("devices", api.devices);
  const topoQ = useResource("topology", api.topology);
  const devices = devicesQ.data ?? [];
  const topo = topoQ.data ?? null;
  const error = devicesQ.error ? "load failed" : "";
  useEffect(() => {
    if (devicesQ.error) toast.fromError("devices failed to load", devicesQ.error);
  }, [devicesQ.error]);

  const role = (d: Device) => topo?.nodes.find((n) => n.name === d.name)?.role ?? "—";

  return (
    <section className="rounded-[0.25rem] border border-edge bg-card">
      <div className="border-b border-edge px-4 py-3 font-mono text-xs uppercase tracking-widest text-[dim-neutral]">
        inventory — {devices.length} devices
      </div>
      {error && <Retry onRetry={() => devicesQ.reload()} note="devices failed to load" />}
      {!error && devices.length === 0 && <Skeleton lines={6} />}
      <table className="w-full font-mono text-xs">
        <thead>
          <tr className="border-b border-edge text-left text-[10px] uppercase tracking-wider text-[dim-neutral]">
            {["name", "role", "platform", "mgmt address", "auth", "clab node"].map((h) => (
              <th key={h} className="px-4 py-2">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {devices.map((d) => (
            <tr key={d.id} className="border-b border-edge/50 hover:bg-panel">
              <td className="px-4 py-2 text-accent-soft">{d.name}</td>
              <td className="px-4 py-2 text-secondary">{role(d)}</td>
              <td className="px-4 py-2">{d.platform}</td>
              <td className="px-4 py-2 text-[dim-neutral]">{d.mgmt_ip}:{d.port}</td>
              <td className="px-4 py-2 text-[dim-neutral]">{d.auth_ref}</td>
              <td className="px-4 py-2 text-[dim-neutral]">{d.containerlab_node ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {devices.length === 0 && !error && (
        <div className="p-4 text-center font-mono text-xs text-[dimmer-neutral]">no devices registered</div>
      )}
    </section>
  );
}
