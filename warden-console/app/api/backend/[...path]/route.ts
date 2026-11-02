import { accessToken } from "@/lib/session";
import { BACKEND_URL, relay } from "@/lib/upstream";

type Ctx = { params: Promise<{ path: string[] }> };

// Only the endpoints the console uses are relayed; anything else is a 404. Each call carries
// the signed-in person's access token, which the backend verifies itself.
const GET_ALLOWED = [/^health$/, /^me$/, /^policies$/, /^runs$/, /^runs\/[\w-]+$/, /^inbox$/];
const POST_ALLOWED = [/^runs$/, /^runs\/image$/, /^runs\/[\w-]+\/(resume|retry)$/, /^inbox\/[\w.-]+\/submit$/];

function target(path: string[], search: string, allowed: RegExp[]): string | null {
  const joined = path.join("/");
  return allowed.some((r) => r.test(joined)) ? `${BACKEND_URL}/${joined}${search}` : null;
}

export async function GET(req: Request, { params }: Ctx) {
  const url = target((await params).path, new URL(req.url).search, GET_ALLOWED);
  if (!url) return Response.json({ detail: "not found" }, { status: 404 });
  const token = await accessToken();
  if (!token) return Response.json({ detail: "sign in first" }, { status: 401 });
  return relay(url, { headers: { authorization: `Bearer ${token}` } });
}

export async function POST(req: Request, { params }: Ctx) {
  const url = target((await params).path, "", POST_ALLOWED);
  if (!url) return Response.json({ detail: "not found" }, { status: 404 });
  const token = await accessToken();
  if (!token) return Response.json({ detail: "sign in first" }, { status: 401 });
  return relay(url, { method: "POST", headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
                      body: await req.text() });
}
