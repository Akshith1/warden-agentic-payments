import { cookies } from "next/headers";
import { CONSOLE_URL, PKCE, REDIRECT_URI, exchange, saveTokens } from "@/lib/session";

export async function GET(req: Request) {
  const params = new URL(req.url).searchParams;
  const jar = await cookies();
  const saved = jar.get(PKCE)?.value;
  jar.delete(PKCE);
  const pkce = saved ? (JSON.parse(saved) as { verifier: string; state: string; next: string }) : null;
  if (!pkce || params.get("state") !== pkce.state || !params.get("code")) {
    return new Response("Sign-in expired or was tampered with. Go back and sign in again.", { status: 400 });
  }
  try {
    await saveTokens(await exchange({ grant_type: "authorization_code", code: params.get("code")!,
                                      redirect_uri: REDIRECT_URI, code_verifier: pkce.verifier }));
  } catch (e) {
    return new Response(e instanceof Error ? e.message : String(e), { status: 502 });
  }
  return Response.redirect(new URL(pkce.next, CONSOLE_URL), 302);
}
