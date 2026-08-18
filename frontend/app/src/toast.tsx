import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

// Global toast system (§2): every failed api.* call surfaces the REAL
// error — backend typed errors get readable copy instead of a raw dump —
// and mutating actions confirm with a success line. Errors never vanish
// into .catch(() => {}) again.

export type ToastKind = "ok" | "err" | "warn";

interface Toast {
  id: number;
  kind: ToastKind;
  title: string;
  body?: string;
}

interface ToastApi {
  ok: (title: string, body?: string) => void;
  err: (title: string, body?: string) => void;
  warn: (title: string, body?: string) => void;
  fromError: (action: string, e: unknown) => void;
}

const Ctx = createContext<ToastApi | null>(null);

/** Map a backend typed error to readable copy (restor8_core/models.py). */
export function describeError(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e);
  // gateway passes backend details through as "NNN: {json}" — unpack
  const m = raw.match(/\d{3}: ([\s\S]*)/);
  let payload: Record<string, unknown> = {};
  try {
    const inner = m?.[1] ?? "";
    payload = JSON.parse(inner.startsWith("{") ? inner : "{}");
  } catch { /* not a structured detail */ }
  const kind = String(payload.error ?? "");
  const msg = String(payload.message ?? payload.detail ?? raw);
  const device = payload.device ? ` on ${payload.device}` : "";
  switch (kind) {
    case "DeviceUnreachableError":
      return `Device unreachable${device} — wrong mgmt address, NETCONF not enabled, or port filtered. (${msg})`;
    case "AuthenticationFailedError":
      return `Credentials rejected${device} — check the device's auth_ref Secret. (${msg})`;
    case "LockFailedError":
      return `Another session holds the config lock${device} — wait or close it. (${msg})`;
    case "CommitFailedError":
      return `Commit rejected by the device${device} — Junos said: ${msg}`;
    case "LoadFailedError":
      return `Config load failed${device} — likely a syntax error in the payload. (${msg})`;
    case "DeviceRpcTimeoutError":
      return `RPC timed out${device} — device slow or overloaded. (${msg})`;
    case "Restor8Error":
      return `Device error${device}: ${msg}`;
    default:
      return raw;
  }
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback((kind: ToastKind, title: string, body?: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t.slice(-4), { id, kind, title, body }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "err" ? 9000 : 4500);
  }, []);

  const api: ToastApi = {
    ok: (t, b) => push("ok", t, b),
    err: (t, b) => push("err", t, b),
    warn: (t, b) => push("warn", t, b),
    fromError: (action, e) => push("err", action, describeError(e)),
  };

  return (
    <Ctx.Provider value={api}>
      {children}
      <div aria-live="polite" className="fixed bottom-4 right-4 z-50 grid max-w-md gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            role="status"
            className={`rounded-[0.25rem] border bg-card px-3 py-2 font-mono text-xs ${
              t.kind === "ok" ? "border-ok/50 text-ok" : t.kind === "warn" ? "border-warn/50 text-warn" : "border-err/50 text-err"
            }`}
          >
            <div>{t.title}</div>
            {t.body && <div className="mt-1 break-words text-dim-neutral">{t.body}</div>}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useToast outside ToastProvider");
  return ctx;
}
