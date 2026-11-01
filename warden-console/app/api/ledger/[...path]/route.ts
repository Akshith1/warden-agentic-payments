import { LEDGER_URL, VENDOR_LOOKUP_KEY, relay } from "@/lib/upstream";

type Ctx = { params: Promise<{ path: string[] }> };

// Read-only views of the ledger. The console never holds the ledger's write key.
const ALLOWED = [/^accounts$/, /^transactions$/, /^vendors\/lookup$/];

export async function GET(req: Request, { params }: Ctx) {
  const joined = (await params).path.join("/");
  if (!ALLOWED.some((r) => r.test(joined))) return Response.json({ detail: "not found" }, { status: 404 });
  return relay(`${LEDGER_URL}/${joined}${new URL(req.url).search}`, { headers: { "X-API-Key": VENDOR_LOOKUP_KEY } });
}
