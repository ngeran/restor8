import { useState } from "react";
import Dashboard from "./Dashboard";
import Devices from "./Devices";
import Configurations from "./Configurations";
import Topology from "./Topology";

const TABS = ["dashboard", "devices", "configurations", "topology"] as const;
type Tab = (typeof TABS)[number];

export default function App() {
  const [tab, setTab] = useState<Tab>("dashboard");
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
                    : "text-[#8b97b8] hover:text-accent-soft"
                }`}
              >
                {t}
              </button>
            ))}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-6">
        {tab === "dashboard" && <Dashboard onGoto={(t) => setTab(t as Tab)} />}
        {tab === "devices" && <Devices />}
        {tab === "configurations" && <Configurations />}
        {tab === "topology" && <Topology />}
      </main>
    </div>
  );
}
