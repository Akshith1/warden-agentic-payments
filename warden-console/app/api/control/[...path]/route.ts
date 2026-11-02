import { accessToken } from "@/lib/session";
import { CONTROL_URL, relay } from "@/lib/upstream";

type Ctx = { params: Promise<{ path: string[] }> };

// The kill switch and the audit log. warden-control checks the person's token and role itself:
// approvers and controllers may stop payments, only controllers may resume them.
const GET_ALLOWED = [/^status$/, /^audit$/, /^audit\/verify$/, /^spend$/];
const POST_ALLOWED = [/^kill-switch\/(trip|reset)$/];

async function forward(req: Request, ctx: Ctx, allowed: RegExp[], init: RequestInit) {
  const joined = (await ctx.params).path.join("/");
  if (!allowed.some((r) => r.test(joined))) return Response.json({ detail: "not found" }, { status: 404 });
  const token = await accessToken();
  if (!token) return Response.json({ detail: "sign in first" }, { status: 401 });
  const headers = { ...(init.headers as Record<string, string>), authorization: `Bearer ${token}` };
  return relay(`${CONTROL_URL}/${joined}${new URL(req.url).search}`, { ...init, headers });
}

export const GET = (req: Request, ctx: Ctx) => forward(req, ctx, GET_ALLOWED, {});
export const POST = async (req: Request, ctx: Ctx) =>
  forward(req, ctx, POST_ALLOWED, { method: "POST", headers: { "content-type": "application/json" }, body: await req.text() });
