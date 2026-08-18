import { useEffect, useRef, useState } from "react";

// The live feed: gateway WS with optional filters. Reconnects with
// exponential backoff + jitter (1s→2s→4s→8s, capped at 15s, ±20%) and
// exposes the reconnect state so the UI can say WHY it's quiet.
export interface LiveEvent {
  session_id?: string;
  device?: string;
  stage?: string;
  message?: string;
  run?: number;
  phase?: string;
  scenario?: string;
  ts?: number;
}

export type WsState =
  | { kind: "live" }
  | { kind: "reconnecting"; inSecs: number; attempt: number };

const BASE_DELAY = 1000;
const MAX_DELAY = 15000;

function backoffDelay(attempt: number): number {
  const raw = Math.min(BASE_DELAY * 2 ** attempt, MAX_DELAY);
  const jitter = raw * (0.8 + Math.random() * 0.4); // ±20%
  return Math.round(jitter);
}

export function useEvents(filters?: Record<string, string>) {
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [state, setState] = useState<WsState>({ kind: "live" });
  const filtersKey = JSON.stringify(filters ?? {});
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const qs = filters ? new URLSearchParams(filters).toString() : "";
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let closed = false;
    let timer: number | undefined;
    let attempt = 0;

    const scheduleReconnect = () => {
      const delay = backoffDelay(attempt);
      attempt += 1;
      setState({ kind: "reconnecting", inSecs: Math.round(delay / 1000), attempt });
      timer = window.setTimeout(connect, delay);
    };
    const tick = () => {
      // keep the "in Ns" countdown honest while we wait
      timer = window.setTimeout(() => {
        setState((s) =>
          s.kind === "reconnecting" && s.inSecs > 1
            ? { ...s, inSecs: s.inSecs - 1 }
            : s,
        );
        if (!closed) tick();
      }, 1000);
    };

    const connect = () => {
      const ws = new WebSocket(`${proto}://${location.host}/ws${qs ? `?${qs}` : ""}`);
      wsRef.current = ws;
      ws.onopen = () => {
        attempt = 0;
        setState({ kind: "live" });
        if (timer) clearTimeout(timer);
      };
      ws.onclose = () => {
        if (!closed) {
          scheduleReconnect();
          tick();
        }
      };
      ws.onmessage = (m) => {
        try {
          const ev: LiveEvent = JSON.parse(m.data);
          ev.ts = ev.ts ?? Date.now() / 1000;
          setEvents((prev) => [...prev.slice(-99), ev]);
        } catch {
          /* non-JSON frame — ignore */
        }
      };
    };
    connect();
    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      wsRef.current?.close();
    };
  }, [filtersKey]);

  return { events, state };
}
