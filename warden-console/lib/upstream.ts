// Server-only: the console's Next.js server talks to the backend and ledger on the
// Docker network. The browser only ever calls /api/*, so the ledger read key never
// leaves the server.
export const BACKEND_URL = process.env.BACKEND_URL ?? "http://warden-backend:8000";
export const LEDGER_URL = process.env.LEDGER_URL ?? "http://warden-ledger:8000";
export const CONTROL_URL = process.env.CONTROL_URL ?? "http://warden-control:8000";
export const QUALITY_URL = process.env.QUALITY_URL ?? "http://warden-quality:8000";
export const VENDOR_LOOKUP_KEY = process.env.VENDOR_LOOKUP_KEY ?? "";
// Phase 11b-2: on Azure the backend admits only requests with a front-door key; this is the console's.
const BACKEND_ENTRY_KEY = process.env.BACKEND_ENTRY_KEY ?? "";

export async function relay(url: string, init: RequestInit = {}): Promise<Response> {
  try {
    const headers = BACKEND_ENTRY_KEY && url.startsWith(BACKEND_URL)
      ? { ...(init.headers as Record<string, string>), "x-warden-entry": BACKEND_ENTRY_KEY } : init.headers;
    const upstream = await fetch(url, { ...init, headers, cache: "no-store" });
    const body = await upstream.text();
    return new Response(body, {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
    });
  } catch {
    return Response.json({ detail: `Can't reach ${new URL(url).host}. Is the stack running?` }, { status: 502 });
  }
}
