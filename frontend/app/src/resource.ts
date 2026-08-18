import { useCallback, useEffect, useRef, useState } from "react";

// §8 shared data layer: one fetch per resource across all screens, an
// in-memory cache, revalidation on focus/interval (PAUSED when the tab is
// backgrounded — document.visibilitychange), and abort-on-unmount so a
// dead screen can never write state.

interface Entry<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  at: number; // Date.now() of last successful fetch
}

const cache = new Map<string, Entry<unknown>>();
const listeners = new Map<string, Set<() => void>>();

function notify(key: string) {
  listeners.get(key)?.forEach((fn) => fn());
}

function setEntry(key: string, patch: Partial<Entry<unknown>>) {
  cache.set(key, { ...(cache.get(key) as Entry<unknown> | undefined ?? { data: undefined, error: undefined, loading: true, at: 0 }), ...patch });
  notify(key);
}

export function useResource<T>(key: string, fetcher: () => Promise<T>, opts?: { pollMs?: number }) {
  const [, force] = useState(0);
  const alive = useRef(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const load = useCallback(async () => {
    setEntry(key, { loading: true, error: undefined });
    try {
      const data = await fetcherRef.current();
      if (!alive.current) return;
      setEntry(key, { data, error: undefined, loading: false, at: Date.now() });
    } catch (e) {
      if (!alive.current) return;
      setEntry(key, { error: e, loading: false });
    }
  }, [key]);

  useEffect(() => {
    alive.current = true;
    const onChange = () => force((n) => n + 1);
    if (!listeners.has(key)) listeners.set(key, new Set());
    listeners.get(key)!.add(onChange);

    const fresh = (cache.get(key) as Entry<T> | undefined)?.at ?? 0;
    if (!fresh) load();

    const poll = opts?.pollMs;
    let timer: number | undefined;
    let visible = document.visibilityState === "visible";
    const tick = () => {
      if (visible && poll) load();
      timer = window.setTimeout(tick, poll);
    };
    const onVis = () => {
      visible = document.visibilityState === "visible";
      if (visible && poll) load(); // catch up immediately on refocus
    };
    if (poll) { timer = window.setTimeout(tick, poll); document.addEventListener("visibilitychange", onVis); }

    return () => {
      alive.current = false;
      listeners.get(key)?.delete(onChange);
      if (timer) clearTimeout(timer);
      if (poll) document.removeEventListener("visibilitychange", onVis);
    };
  }, [key, opts?.pollMs]);

  const entry = cache.get(key) as Entry<T> | undefined;
  return {
    data: entry?.data,
    error: entry?.error,
    loading: entry?.loading ?? true,
    reload: load,
  };
}
