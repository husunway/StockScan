/**
 * Signed session cookie: base64url(JSON{u, exp}) + "." + base64url(HMAC-SHA256).
 * Uses Web Crypto only so it runs in proxy.ts and in route handlers alike.
 * api/index.py verifies the same format.
 */
export const SESSION_COOKIE = "stockscan_session";
export const SESSION_TTL_SECONDS = 7 * 24 * 60 * 60;

const enc = new TextEncoder();

function toB64Url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromB64Url(s: string): Uint8Array {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

async function sign(payload: string, secret: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(payload));
  return toB64Url(new Uint8Array(sig));
}

function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export async function createSessionToken(username: string, secret: string): Promise<string> {
  const exp = Math.floor(Date.now() / 1000) + SESSION_TTL_SECONDS;
  const payload = toB64Url(enc.encode(JSON.stringify({ u: username, exp })));
  return `${payload}.${await sign(payload, secret)}`;
}

export async function verifySessionToken(
  token: string | undefined,
  secret: string | undefined
): Promise<{ username: string } | null> {
  if (!token || !secret) return null;
  const dot = token.indexOf(".");
  if (dot <= 0) return null;
  const payload = token.slice(0, dot);
  const sig = token.slice(dot + 1);
  if (!safeEqual(await sign(payload, secret), sig)) return null;
  try {
    const data = JSON.parse(new TextDecoder().decode(fromB64Url(payload)));
    if (typeof data.exp !== "number" || data.exp < Date.now() / 1000) return null;
    return { username: String(data.u) };
  } catch {
    return null;
  }
}
