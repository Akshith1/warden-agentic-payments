"use client";
import { useCallback, useEffect, useRef, useState } from "react";

// Polls a JSON endpoint while `active`. Polling (not a WebSocket) is a deliberate
// phase 3 simplification: runs change every few seconds, and a 1.5 s poll is enough.
export function useLive<T>(url: string, intervalMs = 1500, active = true) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(url, { cache: "no-store" });
      const body = await res.json();
      if (!res.ok) throw new Error(body?.detail ?? `HTTP ${res.status}`);
      setData(body as T);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [url]);

  useEffect(() => {
    let stopped = false;
    const tick = async () => {
      await load();
      if (!stopped && active) timer.current = setTimeout(tick, intervalMs);
    };
    tick();
    return () => {
      stopped = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [load, intervalMs, active]);

  return { data, error, reload: load };
}

export async function post<T = unknown>(url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json?.detail ?? `HTTP ${res.status}`);
  return json as T;
}
