import { NextResponse, type NextRequest } from "next/server";
import { SESSION_COOKIE, verifySessionToken } from "@/lib/session";

// Paths that should answer 401 instead of redirecting to the login page.
const API_PREFIXES = ["/api/", "/docs", "/openapi.json"];

export async function proxy(req: NextRequest) {
  const { pathname, search } = req.nextUrl;
  const session = await verifySessionToken(
    req.cookies.get(SESSION_COOKIE)?.value,
    process.env.AUTH_SECRET
  );

  if (pathname === "/login") {
    return session ? NextResponse.redirect(new URL("/", req.url)) : NextResponse.next();
  }
  if (session) return NextResponse.next();

  if (API_PREFIXES.some((p) => pathname.startsWith(p))) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }
  const login = new URL("/login", req.url);
  login.searchParams.set("next", pathname + search);
  return NextResponse.redirect(login);
}

export const config = {
  matcher: ["/((?!api/auth/|_next/static|_next/image|favicon.ico).*)"],
};
