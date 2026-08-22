import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Device, type PlanNode, type Topology } from "./api";
import { useEvents } from "./events";
import { useResource } from "./resource";

// The lab map. DISCOVERED is the default view: nodes from inventory, links
// inferred from live device configuration (two devices holding host addrs
// of one /30 = a link) — the objective's "topology built from the
// configuration". The plan stays as a comparison overlay and the apply
// intent. Full-bleed canvas; layout persists per topology.

interface Pos { x: number; y: number }
interface DiscLink { a: string; a_if: string; a_ip: string; b: string | null; b_if: string | null; b_ip: string | null; segment: string; state: string }

const ROLE_RADIUS: Record<string, number> = { P: 26, PE: 22, RR: 22, CE: 18 };
// OLED-friendly: luminance spread across channels instead of one hot hue
const ROLE_COLOR: Record<string, string> = { P: "#59c2ff", PE: "#7ce38b", RR: "#ffb454", CE: "#ffd173" };

export default function Topology({ onSelectDevice }: { onSelectDevice?: (name: string) => void }) {
  const { events } = useEvents();
  const devicesQ = useResource("devices", api.devices);
  const ifacesQ = useResource("interfaces", api.interfaces, { pollMs: 30000 });
  const planQ = useResource("topology", api.topology);
  const discQ = useResource("discovered", api.discovered, { pollMs: 30000 });
  const [source, setSource] = useState<"live" | "plan">("live");
  const mgmtOf = new Map((devicesQ.data ?? []).map((d: Device) => [d.name, d.mgmt_ip]));
  const liveIfaces = ifacesQ.data?.devices ?? {};
  const discovered = discQ.data;
  const plan = planQ.data ?? null;

  const [pos, setPos] = useState<Record<string, Pos>>({});
  const drag = useRef<{ name: string; dx: number; dy: number } | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  // full real estate: viewBox tracks the container's actual size
  const [vb, setVb] = useState({ w: 1200, h: 700 });
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) setVb({ w: Math.round(r.width), h: Math.round(r.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const pan = useRef<{ x: number; y: number } | null>(null);

  // which topo we render: discovered (from live config) or the plan
  const topo: Topology | null = useMemo(() => {
    if (source === "plan") return plan;
    if (!discovered || !discovered.nodes.length) return plan;
    const role = (n: string) => plan?.nodes.find((p) => p.name === n);
    return {
      name: "discovered", underlay: "live configuration",
      nodes: discovered.nodes.map((n) => ({
        name: n.name, role: role(n.name)?.role ?? "?",
        asn: role(n.name)?.asn ?? 0, loopback: role(n.name)?.loopback ?? "?",
      })),
      links: discovered.links
        .filter((l) => l.state === "up" && l.b)
        .map((l) => ({ a: l.a, a_if: l.a_if, a_ip: l.a_ip, b: l.b!, b_if: l.b_if ?? "?", b_ip: l.b_ip ?? "" })),
    };
  }, [source, plan, discovered]);

  const dangling = source === "live" && discovered
    ? discovered.links.filter((l) => l.state === "dangling") : [];
  const unreachable = source === "live" && discovered
    ? discovered.nodes.filter((n) => n.state !== "up").map((n) => n.name) : [];

  // layout: seeded ring; PERSISTED per topology (v2 coordinate space)
  const storageKey = topo ? `restor8.layout.v2.${topo.name}` : "";
  const layout = useMemo(() => {
    if (!topo) return null;
    let saved: Record<string, Pos> | null = null;
    try { saved = JSON.parse(localStorage.getItem(storageKey) ?? "null"); } catch { saved = null; }
    if (saved && Object.keys(saved).length) { setPos(saved); return saved; }
    const core = topo.nodes.filter((n) => n.role === "P");
    const edge = topo.nodes.filter((n) => n.role !== "P");
    const cx = vb.w / 2, cy = vb.h / 2;
    const p: Record<string, Pos> = {};
    core.forEach((n, i) => {
      const a = (i / core.length) * 2 * Math.PI - Math.PI / 2;
      p[n.name] = { x: cx + vb.h * 0.22 * Math.cos(a), y: cy + vb.h * 0.22 * Math.sin(a) };
    });
    edge.forEach((n, i) => {
      const a = (i / edge.length) * 2 * Math.PI - Math.PI / 2;
      p[n.name] = { x: cx + vb.w * 0.38 * Math.cos(a), y: cy + vb.h * 0.42 * Math.sin(a) };
    });
    setPos(p);
    return p;
  }, [topo, storageKey, vb.w, vb.h]);
  useEffect(() => {
    if (storageKey && Object.keys(pos).length) localStorage.setItem(storageKey, JSON.stringify(pos));
  }, [pos, storageKey]);

  // live glow: any WS event naming this device within 5s
  const lastSeen: Record<string, number> = {};
  for (const e of events) if (e.device) lastSeen[e.device.split(".")[0]] = e.ts ?? 0;
  const now = Date.now() / 1000;

  const pt = (svg: SVGSVGElement, ev: React.PointerEvent) => {
    const r = svg.getBoundingClientRect();
    return { x: ev.clientX - r.left, y: ev.clientY - r.top };
  };

  if (!topo || !layout) {
    return <div className="p-4 font-mono text-xs text-dimmer-neutral">loading topology…</div>;
  }

  return (
    <section className="rounded-[0.25rem] border border-edge bg-card">
      <div className="flex flex-wrap items-center gap-3 border-b border-edge px-4 py-2 font-mono text-[10px] uppercase tracking-widest text-dim-neutral">
        <span>
          {source === "live" ? "discovered from live config" : "plan"} — {topo.nodes.length} nodes / {topo.links.length} links
          {source === "live" && dangling.length > 0 && <span className="text-warn"> · {dangling.length} dangling</span>}
          {source === "live" && unreachable.length > 0 && <span className="text-err"> · unreachable: {unreachable.join(", ")}</span>}
        </span>
        <div className="ml-auto flex gap-1">
          {(["live", "plan"] as const).map((s) => (
            <button key={s} onClick={() => setSource(s)}
              className={`rounded-[0.25rem] px-2 py-0.5 ${source === s ? "bg-accent/10 text-accent" : "text-dim-neutral hover:text-accent-soft"}`}>
              {s === "live" ? "◉ live (discovered)" : "▤ plan"}
            </button>
          ))}
        </div>
      </div>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${vb.w} ${vb.h}`}
        className="h-[calc(100vh-230px)] min-h-[420px] w-full touch-none select-none"
        onWheel={(e) => {
          e.preventDefault();
          const factor = e.deltaY < 0 ? 1.1 : 0.9;
          setView((v) => ({ ...v, k: Math.min(3, Math.max(0.4, v.k * factor)) }));
        }}
        onPointerDown={(e) => { if (!drag.current) pan.current = { x: e.clientX, y: e.clientY }; }}
        onPointerMove={(e) => {
          if (drag.current && svgRef.current) {
            const { x, y } = pt(svgRef.current, e);
            const { name, dx, dy } = drag.current;
            setPos((p) => ({ ...p, [name]: { x: x - dx, y: y - dy } }));
          } else if (pan.current) {
            const dx = e.clientX - pan.current.x, dy = e.clientY - pan.current.y;
            pan.current = { x: e.clientX, y: e.clientY };
            setView((v) => ({ ...v, x: v.x + dx / v.k, y: v.y + dy / v.k }));
          }
        }}
        onPointerUp={() => { drag.current = null; pan.current = null; }}
        onPointerLeave={() => { drag.current = null; pan.current = null; }}
      >
        <g transform={`translate(${-view.x} ${-view.y}) scale(${view.k}) translate(${view.x} ${view.y})`}>
          {dangling.map((l, i) => {
            const a = pos[l.a];
            if (!a) return null;
            return (
              <line key={`d${i}`} x1={a.x} y1={a.y} x2={a.x + 40} y2={a.y + 30}
                stroke="#ffd173" strokeWidth={1.5} strokeDasharray="4 4">
                <title>{`DANGLING ${l.segment} — ${l.a} ${l.a_if} (${l.a_ip}) configured but unpaired`}</title>
              </line>
            );
          })}
          {topo.links.map((l, i) => {
            const a = pos[l.a], b = pos[l.b];
            if (!a || !b) return null;
            const hot = now - (lastSeen[l.a] ?? 0) < 5 || now - (lastSeen[l.b] ?? 0) < 5;
            return (
              <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                stroke={hot ? "#59c2ff" : "#16161c"} strokeWidth={hot ? 2 : 1.25}
                strokeDasharray={hot ? "6 4" : undefined} className="cursor-help transition-all">
                <title>{linkTitle(l)}</title>
              </line>
            );
          })}
          {topo.nodes.map((n) => {
            const p = pos[n.name];
            if (!p) return null;
            const active = now - (lastSeen[n.name] ?? 0) < 5;
            const dead = unreachable.includes(n.name);
            const r = ROLE_RADIUS[n.role] ?? 20;
            return (
              <g key={n.name} transform={`translate(${p.x} ${p.y})`} className="cursor-grab"
                onPointerDown={(e) => {
                  if (!svgRef.current) return;
                  const m = pt(svgRef.current, e);
                  drag.current = { name: n.name, dx: m.x - p.x, dy: m.y - p.y };
                }}
                onClick={() => { if (Math.abs(drag.current?.dx ?? 0) < 2) onSelectDevice?.(n.name); }}
                style={{ cursor: "grab" }}>
                <title>{nodeTitle(n, liveIfaces[n.name], topo, mgmtOf.get(n.name))}</title>
                <circle r={r} fill="#050505"
                  stroke={dead ? "#ff6b6b" : active ? "#59c2ff" : planMismatch(n.name, topo, liveIfaces) ? "#ffd173" : ROLE_COLOR[n.role] ?? "#16161c"}
                  strokeWidth={active || dead ? 2.5 : 1.5}
                  strokeDasharray={dead ? "6 4" : undefined}
                  className={active ? "glow-accent" : undefined} />
                <text textAnchor="middle" dy="0.35em" className="fill-[#b8c2cc] font-mono"
                  fontSize={n.role === "P" ? 12 : 11}>{n.name}</text>
                <text textAnchor="middle" dy={r + 12} className="fill-[#4a5261] font-mono" fontSize={9}>
                  {n.role} · as{n.asn}
                </text>
              </g>
            );
          })}
        </g>
      </svg>
      <div className="flex flex-wrap items-center gap-3 border-t border-edge px-4 py-2 font-mono text-[10px] text-dim-neutral">
        {Object.entries(ROLE_COLOR).map(([role, color]) => (
          <span key={role} className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full" style={{ background: color }} />
            {role}
          </span>
        ))}
        <span className="ml-auto">◉ live · ▤ plan · scroll zoom · drag bg pan · hover = interfaces+IPs</span>
      </div>
    </section>
  );
}

/** "10.10.0.0/30 — p1 eth1 (10.10.0.1) ↔ p2 eth1 (10.10.0.2)" */
function linkTitle(l: { a: string; a_if: string; a_ip: string; b: string; b_if: string; b_ip: string }): string {
  const seg = netOf(l.a_ip);
  return `${seg} — ${l.a} ${l.a_if} (${l.a_ip.split("/")[0]}) ↔ ${l.b} ${l.b_if} (${l.b_ip?.split("/")[0] ?? "?"})`;
}

/** Network address of a /30 (plan convention: .1/.2 hosts). */
function netOf(ipWithPrefix: string): string {
  const [ip, len] = ipWithPrefix.split("/");
  if (len !== "30") return ipWithPrefix;
  const o = ip.split(".");
  return `${o[0]}.${o[1]}.${o[2]}.0/30`;
}

/** Node hover: identity + plan + LIVE per-interface state with drift marks. */
function nodeTitle(
  n: PlanNode,
  live: { interfaces?: Record<string, { addrs: string[]; oper: string | null }>; error?: string } | undefined,
  topo: Topology,
  mgmt: string | undefined,
): string {
  const head = `${n.name} — ${n.role} · AS${n.asn} · lo0 ${n.loopback} · mgmt ${mgmt ?? "?"}`;
  if (!live) return head + "\n(live state: no data)";
  if (live.error) return head + `\n(live state: unreachable — ${live.error.slice(0, 60)})`;
  const ifs = live.interfaces ?? {};
  const lines: string[] = [];
  for (const l of topo.links) {
    if (l.a === n.name) {
      const got = ifs[l.a_if]?.addrs ?? [];
      const ok = got.includes(l.a_ip);
      lines.push(`  ${l.a_if} → ${l.b}: ${ok ? `${l.a_ip} ✓` : got.length ? `${got.join(", ")} ✗ (planned ${l.a_ip})` : "no address ✗"}`);
    } else if (l.b === n.name) {
      const got = ifs[l.b_if]?.addrs ?? [];
      const ok = got.includes(l.b_ip);
      lines.push(`  ${l.b_if} → ${l.a}: ${ok ? `${l.b_ip} ✓` : got.length ? `${got.join(", ")} ✗ (planned ${l.b_ip})` : "no address ✗"}`);
    }
  }
  const lo = ifs["lo"]?.addrs.filter((a) => a !== "127.0.0.1/8") ?? [];
  lines.push(`  lo0: ${lo.length ? lo.join(", ") : "missing ✗"} (planned ${n.loopback}/32)`);
  return head + "\n" + lines.join("\n");
}

/** True when any planned interface of this node lacks its planned address. */
function planMismatch(name: string, topo: Topology, live: Record<string, { interfaces?: Record<string, { addrs: string[]; oper: string | null }>; error?: string }>): boolean {
  const ifs = live[name]?.interfaces;
  if (!ifs) return false;
  if (live[name]?.error) return true;
  for (const l of topo.links) {
    if (l.a === name && !(ifs[l.a_if]?.addrs ?? []).includes(l.a_ip)) return true;
    if (l.b === name && !(ifs[l.b_if]?.addrs ?? []).includes(l.b_ip)) return true;
  }
  const node = topo.nodes.find((x) => x.name === name);
  if (node && !(ifs["lo"]?.addrs ?? []).includes(`${node.loopback}/32`)) return true;
  return false;
}
