import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Topology } from "./api";
import { useEvents } from "./events";

// Draggable SVG canvas of the planned fabric; nodes glow accent when a
// live event names them (the "live status" of spec §3/§7).

interface Pos { x: number; y: number }

const ROLE_RADIUS: Record<string, number> = { P: 26, PE: 22, RR: 22, CE: 18 };
// OLED-friendly: luminance spread across channels instead of one hot hue
const ROLE_COLOR: Record<string, string> = { P: "#59c2ff", PE: "#7ce38b", RR: "#ffb454", CE: "#ffd173" };

export default function Topology({ onSelectDevice }: { onSelectDevice?: (name: string) => void }) {
  const [topo, setTopo] = useState<Topology | null>(null);
  const { events } = useEvents();
  const [pos, setPos] = useState<Record<string, Pos>>({});
  const drag = useRef<{ name: string; dx: number; dy: number } | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  // §7 zoom/pan: view transform + background-drag panning
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const pan = useRef<{ x: number; y: number } | null>(null);
  const [hoverLink, setHoverLink] = useState<number | null>(null);

  useEffect(() => {
    api.topology().then(setTopo).catch(() => {});
  }, []);

  // layout: seeded ring (P-core inner, others outer) and PERSISTED to
  // localStorage per topology name — dragged positions survive tab
  // switches and reloads (§1 fix; backend-stored layout is a stretch goal)
  const storageKey = topo ? `restor8.layout.${topo.name}` : "";
  const layout = useMemo(() => {
    if (!topo) return null;
    let saved: Record<string, Pos> | null = null;
    try {
      saved = JSON.parse(localStorage.getItem(storageKey) ?? "null");
    } catch { saved = null; }
    if (saved && Object.keys(saved).length) {
      setPos(saved);
      return saved;
    }
    const core = topo.nodes.filter((n) => n.role === "P");
    const edge = topo.nodes.filter((n) => n.role !== "P");
    const p: Record<string, Pos> = {};
    core.forEach((n, i) => {
      const a = (i / core.length) * 2 * Math.PI - Math.PI / 2;
      p[n.name] = { x: 450 + 130 * Math.cos(a), y: 260 + 130 * Math.sin(a) };
    });
    edge.forEach((n, i) => {
      const a = (i / edge.length) * 2 * Math.PI - Math.PI / 2;
      p[n.name] = { x: 450 + 260 * Math.cos(a), y: 260 + 240 * Math.sin(a) };
    });
    setPos(p);
    return p;
  }, [topo, storageKey]);

  // save (debounced by React's commit) whenever positions change
  useEffect(() => {
    if (storageKey && Object.keys(pos).length) {
      localStorage.setItem(storageKey, JSON.stringify(pos));
    }
  }, [pos, storageKey]);

  const lastSeen: Record<string, number> = {};
  for (const e of events) if (e.device) lastSeen[e.device.split(".")[0]] = e.ts ?? 0;
  const now = Date.now() / 1000;

  const pt = (svg: SVGSVGElement, ev: React.PointerEvent) => {
    const r = svg.getBoundingClientRect();
    return { x: ((ev.clientX - r.left) / r.width) * 900, y: ((ev.clientY - r.top) / r.height) * 520 };
  };

  if (!topo || !layout) {
    return <div className="p-4 font-mono text-xs text-[dimmer-neutral]">loading topology…</div>;
  }

  return (
    <section className="rounded-[0.25rem] border border-edge bg-card">
      <div className="border-b border-edge px-4 py-3 font-mono text-xs uppercase tracking-widest text-[dim-neutral]">
        {topo.name} — {topo.nodes.length} nodes / {topo.links.length} links ({topo.underlay})
      </div>
      <svg
        ref={svgRef}
        viewBox="0 0 900 520"
        className="h-[520px] w-full touch-none select-none"
        onWheel={(e) => {
          e.preventDefault();
          const factor = e.deltaY < 0 ? 1.1 : 0.9;
          setView((v) => ({ ...v, k: Math.min(3, Math.max(0.4, v.k * factor)) }));
        }}
        onPointerDown={(e) => {
          if (drag.current) return;
          pan.current = { x: e.clientX, y: e.clientY };
        }}
        onPointerMove={(e) => {
          if (drag.current && svgRef.current) {
            const { x, y } = pt(svgRef.current, e);
            const { name, dx, dy } = drag.current;
            setPos((p) => ({ ...p, [name]: { x: x - dx, y: y - dy } }));
          } else if (pan.current) {
            const dx = e.clientX - pan.current.x;
            const dy = e.clientY - pan.current.y;
            pan.current = { x: e.clientX, y: e.clientY };
            setView((v) => ({ ...v, x: v.x + dx / v.k, y: v.y + dy / v.k }));
          }
        }}
        onPointerUp={() => { drag.current = null; pan.current = null; }}
        onPointerLeave={() => { drag.current = null; pan.current = null; }}
      >
        <g transform={`translate(${-view.x} ${-view.y}) scale(${view.k}) translate(${view.x} ${view.y})`}>
        {topo.links.map((l, i) => {
          const a = pos[l.a], b = pos[l.b];
          if (!a || !b) return null;
          const hot =
            now - (lastSeen[l.a] ?? 0) < 5 || now - (lastSeen[l.b] ?? 0) < 5;
          return (
            <line
              key={i}
              x1={a.x} y1={a.y} x2={b.x} y2={b.y}
              stroke={hot ? "#59c2ff" : hoverLink === i ? "#ffb454" : "#16161c"}
              strokeWidth={hot || hoverLink === i ? 2 : 1.25}
              strokeDasharray={hot ? "6 4" : undefined}
              className="cursor-help transition-all"
              onPointerEnter={() => setHoverLink(i)}
              onPointerLeave={() => setHoverLink(null)}
            >
              <title>{`${l.a} ${l.a_if} (${l.a_ip}) ↔ ${l.b} ${l.b_if} (${l.b_ip})`}</title>
            </line>
          );
        })}
        {topo.nodes.map((n) => {
          const p = pos[n.name];
          if (!p) return null;
          const active = now - (lastSeen[n.name] ?? 0) < 5;
          const r = ROLE_RADIUS[n.role] ?? 20;
          return (
            <g
              key={n.name}
              transform={`translate(${p.x} ${p.y})`}
              className="cursor-grab"
              onPointerDown={(e) => {
                if (!svgRef.current) return;
                const m = pt(svgRef.current, e);
                drag.current = { name: n.name, dx: m.x - p.x, dy: m.y - p.y };
              }}
              onClick={(e) => { if (Math.abs(drag.current?.dx ?? 0) < 2) onSelectDevice?.(n.name); }}
              style={{ cursor: "grab" }}
            >
              <circle
                r={r}
                fill="#131a2b"
                stroke={active ? "#59c2ff" : ROLE_COLOR[n.role] ?? "#16161c"}
                strokeWidth={active ? 2.5 : 1.5}
                className={active ? "glow-accent" : undefined}
              />
              <text
                textAnchor="middle" dy="0.35em"
                className="fill-[#b8c2cc] font-mono"
                fontSize={n.role === "P" ? 12 : 11}
              >
                {n.name}
              </text>
              <text
                textAnchor="middle" dy={r + 12}
                className="fill-[dimmer-neutral] font-mono" fontSize={9}
              >
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
        <span className="ml-auto">scroll = zoom · drag bg = pan · drag node = move · hover link = addresses</span>
      </div>
    </section>
  );
}
