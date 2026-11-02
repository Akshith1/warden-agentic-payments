import { CLIENT_ID, CONSOLE_URL, LOGOUT_URL, clearSession } from "@/lib/session";

// Ends the Keycloak session too, so the next sign-in asks for a password again.
export async function GET() {
  const idToken = await clearSession();
  const url = new URL(LOGOUT_URL);
  url.search = new URLSearchParams({ client_id: CLIENT_ID, post_logout_redirect_uri: `${CONSOLE_URL}/`,
                                     ...(idToken ? { id_token_hint: idToken } : {}) }).toString();
  return Response.redirect(url, 302);
}
