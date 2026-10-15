import { NextResponse } from "next/server";
import { createHash, timingSafeEqual } from "crypto";
import { SESSION_COOKIE, SESSION_TTL_SECONDS, createSessionToken } from "@/lib/session";

export const runtime = "nodejs";

// Hash both sides so the comparison is constant-time regardless of length.
const digest = (s: string) => createHash("sha256").update(s, "utf8").digest();
const equal = (a: string, b: string) => timingSafeEqual(digest(a), digest(b));

export async function POST(req: Request) {
  const { AUTH_USERNAME, AUTH_PASSWORD, AUTH_SECRET } = process.env;
  if (!AUTH_USERNAME || !AUTH_PASSWORD || !AUTH_SECRET) {
    return NextResponse.json(
      { detail: "Login is not configured. Set AUTH_USERNAME, AUTH_PASSWORD and AUTH_SECRET." },
      { status: 500 }
    );
  }

  let body: { username?: unknown; password?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ detail: "Invalid request" }, { status: 400 });
  }
  const username = typeof body.username === "string" ? body.username : "";
  const password = typeof body.password === "string" ? body.password : "";

  // Evaluate both so a wrong username and a wrong password take the same time.
  const userOk = equal(username, AUTH_USERNAME);
  const passOk = equal(password, AUTH_PASSWORD);
  if (!(userOk && passOk)) {
    await new Promise((r) => setTimeout(r, 750));
    return NextResponse.json({ detail: "Invalid username or password" }, { status: 401 });
  }

  const res = NextResponse.json({ ok: true });
  res.cookies.set({
    name: SESSION_COOKIE,
    value: await createSessionToken(username, AUTH_SECRET),
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: SESSION_TTL_SECONDS,
  });
  return res;
}
