import { useEffect, useState } from "react";
import { api, type Device, type Topology } from "./api";

export default function Devices() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [topo, setTopo] = useState<Topology | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.devices().then(setDevices).catch((e) => setError(String(e)));
    api.topology().then(setTopo).catch(() => {});
  }, []);

  const role = (d: Device) => topo?.nodes.find((n) => n.name === d.name)?.role ?? "—";

  return (
    <section className="rounded-[0.25rem] border border-edge bg-card">
      <div className="border-b border-edge px-4 py-3 font-mono text-xs uppercase tracking-widest text-[#8b97b8]">
        inventory — {devices.length} devices
      </div>
      {error && <div className="p-4 font-mono text-xs text-err">{error}</div>}
      <table className="w-full font-mono text-xs">
        <thead>
          <tr className="border-b border-edge text-left text-[10px] uppercase tracking-wider text-[#8b97b8]">
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
              <td className="px-4 py-2 text-[#8b97b8]">{d.mgmt_ip}:{d.port}</td>
              <td className="px-4 py-2 text-[#8b97b8]">{d.auth_ref}</td>
              <td className="px-4 py-2 text-[#8b97b8]">{d.containerlab_node ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {devices.length === 0 && !error && (
        <div className="p-4 text-center font-mono text-xs text-[#5b6785]">no devices registered</div>
      )}
    </section>
  );
}
