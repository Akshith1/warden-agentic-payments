// Server-only: sign-in with Keycloak (authorization code + PKCE) and the session cookies.
// The browser never sees a token: they live in httpOnly cookies that only this server reads,
// and every token is verified against the provider's published keys before it is trusted.
// Phase 11: the OIDC_* settings point the same flow at Entra ID on Azure (with a client
// secret, which Entra requires of a web app, and roles in the token's "roles" claim).
import { createHash, randomBytes } from "node:crypto";
import { cookies } from "next/headers";
import { createRemoteJWKSet, decodeJwt, jwtVerify } from "jose";

const PUBLIC = process.env.KEYCLOAK_PUBLIC_URL ?? "http://localhost:8180";     // what browsers use
const INTERNAL = process.env.KEYCLOAK_INTERNAL_URL ?? "http://keycloak:8080";  // what this server uses
const REALM = process.env.KEYCLOAK_REALM ?? "warden";
const env = (name: string, fallback: string) => process.env[name] || fallback;
export const CLIENT_ID = env("OIDC_CLIENT_ID", process.env.KEYCLOAK_CLIENT_ID ?? "warden-console");
const CLIENT_SECRET = process.env.OIDC_CLIENT_SECRET ?? "";   // empty: a public client (Keycloak here)
export const CONSOLE_URL = process.env.CONSOLE_URL ?? "http://localhost:3100";

const KEYCLOAK = `${PUBLIC}/realms/${REALM}/protocol/openid-connect`;
export const ISSUER = env("OIDC_ISSUER", `${PUBLIC}/realms/${REALM}`);
export const AUTHORIZE_URL = env("OIDC_AUTHORIZE_URL", `${KEYCLOAK}/auth`);
export const LOGOUT_URL = env("OIDC_LOGOUT_URL", `${KEYCLOAK}/logout`);
const TOKEN_URL = env("OIDC_TOKEN_URL", `${INTERNAL}/realms/${REALM}/protocol/openid-connect/token`);
const JWKS = createRemoteJWKSet(new URL(env("OIDC_JWKS_URL", `${INTERNAL}/realms/${REALM}/protocol/openid-connect/certs`)));
export const SCOPE = env("OIDC_SCOPE", "openid profile");
const AUDIENCE = env("OIDC_AUDIENCE", "warden-backend");
const ROLES_CLAIM = env("OIDC_ROLES_CLAIM", "realm_access.roles");
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
    body: new URLSearchParams({ client_id: CLIENT_ID, ...(CLIENT_SECRET ? { client_secret: CLIENT_SECRET } : {}),
                                ...form }),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`The sign-in service refused the sign-in (${res.status}): ${await res.text()}`);
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
    const fresh = await exchange({ grant_type: "refresh_token", refresh_token: refresh, scope: SCOPE });
    await saveTokens(fresh);
    return (await verify(fresh.access_token)) ? fresh.access_token : null;
  } catch {
    await clearSession(); // the sign-in session ended: sign in again
    return null;
  }
}

async function verify(token: string): Promise<Person | null> {
  try {
    const { payload } = await jwtVerify(token, JWKS, { issuer: ISSUER, audience: AUDIENCE });
    const found = ROLES_CLAIM.split(".").reduce<unknown>(
      (v, part) => (v && typeof v === "object" ? (v as Record<string, unknown>)[part] : undefined), payload);
    const roles = (Array.isArray(found) ? found : []).filter((r): r is string => WARDEN_ROLES.includes(r));
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
