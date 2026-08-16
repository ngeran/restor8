import { useEffect, useRef, useState } from "react";

// The live feed: gateway WS with optional filters. Reconnects with backoff;
// the bus is in-memory so a dropped run is re-readable via REST anyway.
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

export function useEvents(filters?: Record<string, string>) {
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [live, setLive] = useState(false);
  const filtersKey = JSON.stringify(filters ?? {});
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const qs = filters ? new URLSearchParams(filters).toString() : "";
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let closed = false;
    let timer: number;

    const connect = () => {
      const ws = new WebSocket(`${proto}://${location.host}/ws${qs ? `?${qs}` : ""}`);
      wsRef.current = ws;
      ws.onopen = () => setLive(true);
      ws.onclose = () => {
        setLive(false);
        if (!closed) timer = window.setTimeout(connect, 2000);
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
      clearTimeout(timer);
      wsRef.current?.close();
    };
  }, [filtersKey]);

  return { events, live };
}
