// Small shared UI primitives (§2): skeletons and retry affordances.
export function Skeleton({ lines = 4 }: { lines?: number }) {
  return (
    <div className="grid gap-2 p-4" aria-hidden="true">
      {Array.from({ length: lines }).map((_, i) => (
        <div
          key={i}
          className="animate-pulse rounded-[0.25rem] bg-card"
          style={{ height: "0.75rem", width: `${88 - i * 9}%` }}
        />
      ))}
    </div>
  );
}

export function Retry({ onRetry, note = "failed to load" }: { onRetry: () => void; note?: string }) {
  return (
    <div className="flex items-center gap-3 p-4 font-mono text-xs text-err">
      <span>{note}</span>
      <button
        onClick={onRetry}
        className="rounded-[0.25rem] border border-err/40 px-2 py-0.5 text-err hover:bg-err/10"
      >
        ↻ retry
      </button>
    </div>
  );
}
