import { cookies } from "next/headers";
import { AUTHORIZE_URL, CLIENT_ID, PKCE, REDIRECT_URI, newPkce } from "@/lib/session";

// Starts the sign-in: Keycloak shows its own login page; Warden never sees the password.
export async function GET(req: Request) {
  const next = new URL(req.url).searchParams.get("next") ?? "/";
  const { verifier, challenge, state } = newPkce();
  (await cookies()).set(PKCE, JSON.stringify({ verifier, state, next: next.startsWith("/") ? next : "/" }),
    { httpOnly: true, sameSite: "lax", path: "/api/auth", maxAge: 600 });
  const url = new URL(AUTHORIZE_URL);
  url.search = new URLSearchParams({
    client_id: CLIENT_ID, response_type: "code", scope: "openid profile", redirect_uri: REDIRECT_URI,
    state, code_challenge: challenge, code_challenge_method: "S256",
  }).toString();
  return Response.redirect(url, 302);
}
