import { currentPerson } from "@/lib/session";
import { LEDGER_URL, VENDOR_LOOKUP_KEY, relay } from "@/lib/upstream";

type Ctx = { params: Promise<{ path: string[] }> };

// Read-only views of the ledger. The console never holds the ledger's write key:
// linking a vendor alias goes through the backend's resume endpoint, never from here.
const ALLOWED = [/^accounts$/, /^transactions$/, /^vendors\/lookup$/, /^vendors\/search$/];

export async function GET(req: Request, { params }: Ctx) {
  const joined = (await params).path.join("/");
  if (!ALLOWED.some((r) => r.test(joined))) return Response.json({ detail: "not found" }, { status: 404 });
  // The ledger uses its own read key, so the console checks the person is signed in itself.
  if (!(await currentPerson())) return Response.json({ detail: "sign in first" }, { status: 401 });
  return relay(`${LEDGER_URL}/${joined}${new URL(req.url).search}`, { headers: { "X-API-Key": VENDOR_LOOKUP_KEY } });
}
