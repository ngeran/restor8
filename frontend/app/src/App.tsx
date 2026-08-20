import { useEffect, useState } from "react";
import Dashboard from "./Dashboard";
import Devices from "./Devices";
import Configurations from "./Configurations";
import Topology from "./Topology";
import Labs from "./Labs";

const TABS = ["dashboard", "devices", "labs", "configurations", "topology"] as const;
type Tab = (typeof TABS)[number];

export default function App() {
  const [tab, setTab] = useState<Tab>("dashboard");
  const [focusDevice, setFocusDevice] = useState<string | null>(null);

  // §10: g-then-key tab jumps (terminal muscle memory), Esc clears focus
  useEffect(() => {
    let awaitingG = false;
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT") return;
      if (e.key === "Escape") { setFocusDevice(null); return; }
      if (awaitingG) {
        awaitingG = false;
        const map: Record<string, Tab> = { d: "dashboard", v: "devices", l: "labs", c: "configurations", t: "topology" };
        if (map[e.key]) setTab(map[e.key]);
        return;
      }
      if (e.key === "g") awaitingG = true;
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="min-h-screen bg-base">
      <header className="border-b border-edge bg-panel">
        <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3">
          <span className="font-mono text-lg tracking-widest text-accent">
            restor<span className="text-secondary">8</span>
          </span>
          <nav className="flex gap-1">
            {TABS.map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`rounded-[0.25rem] px-3 py-1 font-mono text-xs uppercase tracking-wider transition-colors ${
                  tab === t
                    ? "bg-card text-accent glow-accent"
                    : "text-[dim-neutral] hover:text-accent-soft"
                }`}
              >
                {t}
              </button>
            ))}
          </nav>
          <span className="ml-auto hidden font-mono text-[10px] text-dimmer-neutral md:inline">g d · g v · g l · g c · g t</span>
        </div>
      </header>
      <main className={tab === "topology" ? "w-full px-4 py-4" : "mx-auto max-w-6xl px-6 py-6"}>
        {tab === "dashboard" && <Dashboard onGoto={(t) => setTab(t as Tab)} />}
        {tab === "devices" && <Devices onSelectDevice={(name) => { setFocusDevice(name); setTab("configurations"); }} />}
        {tab === "labs" && <Labs />}
        {tab === "configurations" && <Configurations initialDevice={focusDevice} />}
        {tab === "topology" && <Topology onSelectDevice={(name) => { setFocusDevice(name); setTab("configurations"); }} />}
      </main>
    </div>
  );
}
