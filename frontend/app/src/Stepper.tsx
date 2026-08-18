import { useEvents } from "./events";

// §4: the push pipeline as a live stepper — lights each stage as its WS
// event arrives for THIS session, error state wins over everything.
// SKILL.md's canonical sequence:
//   resolving → connecting → authenticating → connected → locking →
//   loading-config → diff-ready → committing → commit-confirmed → closed

const STAGES = [
  { key: "resolving", label: "resolve" },
  { key: "connecting", label: "connect" },
  { key: "authenticating", label: "auth" },
  { key: "connected", label: "session" },
  { key: "locking", label: "lock" },
  { key: "loading-config", label: "load" },
  { key: "diff-ready", label: "diff" },
  { key: "committing", label: "commit" },
  { key: "commit-confirmed", label: "confirmed" },
  { key: "closed", label: "closed" },
] as const;

export default function Stepper({ sessionId, onEvent }: { sessionId: string; onEvent?: (stage: string, message: string) => void }) {
  const { events } = useEvents({ session: sessionId });
  const mine = events.filter((e) => e.session_id === sessionId);

  const reached = new Set<string>();
  let failed = false;
  let lastMsg = "";
  for (const e of mine) {
    if (e.stage) { reached.add(e.stage); lastMsg = e.message ?? ""; }
    if (e.stage === "error") failed = true;
  }
  const last = [...reached].pop() ?? "";
  if (onEvent && mine.length) onEvent(last, lastMsg);

  return (
    <div className="flex flex-wrap items-center gap-1" aria-label="push progress">
      {STAGES.map((s) => {
        const hit = reached.has(s.key);
        const isErr = failed && !hit;
        return (
          <span
            key={s.key}
            className={`rounded-[0.25rem] border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider ${
              hit
                ? s.key === "commit-confirmed"
                  ? "border-warn/60 text-warn"
                  : "border-ok/60 text-ok"
                : isErr
                  ? "border-err/40 text-err/60"
                  : "border-edge text-dimmer-neutral"
            }`}
            title={hit ? `${s.key}: ${lastMsg}` : s.key}
          >
            {s.label}
          </span>
        );
      })}
    </div>
  );
}
