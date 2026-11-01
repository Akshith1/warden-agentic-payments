// Server-only: the console's Next.js server talks to the backend and ledger on the
// Docker network. The browser only ever calls /api/*, so the ledger read key never
// leaves the server.
export const BACKEND_URL = process.env.BACKEND_URL ?? "http://warden-backend:8000";
export const LEDGER_URL = process.env.LEDGER_URL ?? "http://warden-ledger:8000";
export const VENDOR_LOOKUP_KEY = process.env.VENDOR_LOOKUP_KEY ?? "";

export async function relay(url: string, init: RequestInit = {}): Promise<Response> {
  try {
    const upstream = await fetch(url, { ...init, cache: "no-store" });
    const body = await upstream.text();
    return new Response(body, {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
    });
  } catch {
    return Response.json({ detail: `Can't reach ${new URL(url).host}. Is the stack running?` }, { status: 502 });
  }
}
