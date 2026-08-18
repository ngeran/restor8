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

export function ConfirmModal({
  title, body, confirmLabel, danger, onConfirm, onCancel,
}: {
  title: string; body: string; confirmLabel: string; danger?: boolean;
  onConfirm: () => void; onCancel: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-40 grid place-items-center bg-black/80 p-4"
      role="dialog" aria-modal="true" aria-label={title}
      onClick={onCancel}
    >
      <div
        className="w-full max-w-md rounded-[0.25rem] border border-edge bg-card p-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className={`mb-2 font-mono text-sm ${danger ? "text-err" : "text-warn"}`}>{title}</div>
        <div className="mb-4 font-mono text-xs leading-5 text-dim-neutral">{body}</div>
        <div className="flex justify-end gap-2">
          <button onClick={onCancel} className="rounded-[0.25rem] border border-edge px-3 py-1 font-mono text-xs text-dim-neutral hover:text-text">
            cancel
          </button>
          <button
            onClick={onConfirm}
            autoFocus
            className={`rounded-[0.25rem] px-3 py-1 font-mono text-xs ${danger ? "bg-err/10 text-err hover:bg-err/20" : "bg-warn/10 text-warn hover:bg-warn/20"}`}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
