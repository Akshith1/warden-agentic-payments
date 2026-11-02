// Server-only: sign-in with Keycloak (authorization code + PKCE) and the session cookies.
// The browser never sees a token: they live in httpOnly cookies that only this server reads,
// and every token is verified against Keycloak's published keys before it is trusted.
import { createHash, randomBytes } from "node:crypto";
import { cookies } from "next/headers";
import { createRemoteJWKSet, decodeJwt, jwtVerify } from "jose";

const PUBLIC = process.env.KEYCLOAK_PUBLIC_URL ?? "http://localhost:8180";     // what browsers use
const INTERNAL = process.env.KEYCLOAK_INTERNAL_URL ?? "http://keycloak:8080";  // what this server uses
const REALM = process.env.KEYCLOAK_REALM ?? "warden";
export const CLIENT_ID = process.env.KEYCLOAK_CLIENT_ID ?? "warden-console";
export const CONSOLE_URL = process.env.CONSOLE_URL ?? "http://localhost:3100";

export const ISSUER = `${PUBLIC}/realms/${REALM}`;
export const AUTHORIZE_URL = `${ISSUER}/protocol/openid-connect/auth`;
export const LOGOUT_URL = `${ISSUER}/protocol/openid-connect/logout`;
const TOKEN_URL = `${INTERNAL}/realms/${REALM}/protocol/openid-connect/token`;
const JWKS = createRemoteJWKSet(new URL(`${INTERNAL}/realms/${REALM}/protocol/openid-connect/certs`));
export const REDIRECT_URI = `${CONSOLE_URL}/api/auth/callback`;

const ACCESS = "wd_at", REFRESH = "wd_rt", ID = "wd_id";
export const PKCE = "wd_pkce";
const WARDEN_ROLES = ["submitter", "approver", "controller"];

export type Person = { username: string; name: string; roles: string[] };

const base64url = (b: Buffer) => b.toString("base64url");
export function newPkce() {
  const verifier = base64url(randomBytes(32));
  return { verifier, challenge: base64url(createHash("sha256").update(verifier).digest()), state: base64url(randomBytes(16)) };
}

const cookieOptions = (maxAge: number) =>
  ({ httpOnly: true, sameSite: "lax" as const, path: "/", maxAge, secure: CONSOLE_URL.startsWith("https") });

type Tokens = { access_token: string; refresh_token?: string; id_token?: string; refresh_expires_in?: number };

export async function exchange(form: Record<string, string>): Promise<Tokens> {
  const res = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: CLIENT_ID, ...form }),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`Keycloak refused the sign-in (${res.status}): ${await res.text()}`);
  return res.json();
}

export async function saveTokens(t: Tokens) {
  const jar = await cookies();
  const lifetime = t.refresh_expires_in ?? 1800;
  jar.set(ACCESS, t.access_token, cookieOptions(lifetime));
  if (t.refresh_token) jar.set(REFRESH, t.refresh_token, cookieOptions(lifetime));
  if (t.id_token) jar.set(ID, t.id_token, cookieOptions(lifetime));
}

export async function clearSession(): Promise<string | undefined> {
  const jar = await cookies();
  const idToken = jar.get(ID)?.value;
  for (const name of [ACCESS, REFRESH, ID, PKCE]) jar.delete(name);
  return idToken;
}

/** A verified access token for the current person, refreshed when close to expiry; null if signed out. */
export async function accessToken(): Promise<string | null> {
  const jar = await cookies();
  const token = jar.get(ACCESS)?.value;
  if (token) {
    const exp = decodeJwt(token).exp ?? 0;
    if (exp * 1000 > Date.now() + 30_000 && (await verify(token))) return token;
  }
  const refresh = jar.get(REFRESH)?.value;
  if (!refresh) return null;
  try {
    const fresh = await exchange({ grant_type: "refresh_token", refresh_token: refresh });
    await saveTokens(fresh);
    return (await verify(fresh.access_token)) ? fresh.access_token : null;
  } catch {
    await clearSession(); // the Keycloak session ended: sign in again
    return null;
  }
}

async function verify(token: string): Promise<Person | null> {
  try {
    const { payload } = await jwtVerify(token, JWKS, { issuer: ISSUER, audience: "warden-backend" });
    const roles = ((payload.realm_access as { roles?: string[] })?.roles ?? []).filter((r) => WARDEN_ROLES.includes(r));
    const username = String(payload.preferred_username ?? payload.sub);
    return { username, name: String(payload.name ?? username), roles };
  } catch {
    return null;
  }
}

export async function currentPerson(): Promise<Person | null> {
  const token = await accessToken();
  return token ? verify(token) : null;
}
