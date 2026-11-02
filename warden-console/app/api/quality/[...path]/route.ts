import { accessToken } from "@/lib/session";
import { QUALITY_URL, relay } from "@/lib/upstream";

type Ctx = { params: Promise<{ path: string[] }> };

// Phase 7b: data-quality results, and "Check now". warden-quality checks the token itself and
// records who asked for a run in the audit log.
const GET_ALLOWED = [/^results$/, /^checks$/];
const POST_ALLOWED = [/^run$/];

async function forward(ctx: Ctx, allowed: RegExp[], init: RequestInit) {
  const joined = (await ctx.params).path.join("/");
  if (!allowed.some((r) => r.test(joined))) return Response.json({ detail: "not found" }, { status: 404 });
  const token = await accessToken();
  if (!token) return Response.json({ detail: "sign in first" }, { status: 401 });
  return relay(`${QUALITY_URL}/${joined}`, { ...init, headers: { authorization: `Bearer ${token}` } });
}

export const GET = (_req: Request, ctx: Ctx) => forward(ctx, GET_ALLOWED, {});
export const POST = (_req: Request, ctx: Ctx) => forward(ctx, POST_ALLOWED, { method: "POST" });
