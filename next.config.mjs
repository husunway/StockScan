import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";

// Build identity shown in the sidebar: package.json version, git commit (Vercel provides VERCEL_GIT_COMMIT_SHA at
// build time; locally it is read from git) and the build time in UTC.
const pkg = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf8"));
function gitSha() {
  if (process.env.VERCEL_GIT_COMMIT_SHA) return process.env.VERCEL_GIT_COMMIT_SHA;
  try {
    return execSync("git rev-parse HEAD", { stdio: ["ignore", "pipe", "ignore"] }).toString().trim();
  } catch {
    return "";
  }
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  env: {
    NEXT_PUBLIC_APP_VERSION: pkg.version,
    NEXT_PUBLIC_GIT_SHA: gitSha().slice(0, 7),
    NEXT_PUBLIC_BUILD_TIME: new Date().toISOString(),
    NEXT_PUBLIC_DEPLOY_ENV: process.env.VERCEL_ENV || "local",
  },
  // In development, proxy Python API calls to the local FastAPI server.
  // On Vercel, /api/*.py files are deployed as Python serverless functions.
  rewrites: async () => [
    {
      source: "/api/py/:path*",
      destination:
        process.env.NODE_ENV === "development"
          ? "http://127.0.0.1:8000/api/py/:path*"
          : "/api/",
    },
    {
      source: "/docs",
      destination:
        process.env.NODE_ENV === "development"
          ? "http://127.0.0.1:8000/api/py/docs"
          : "/api/py/docs",
    },
    {
      source: "/openapi.json",
      destination:
        process.env.NODE_ENV === "development"
          ? "http://127.0.0.1:8000/api/py/openapi.json"
          : "/api/py/openapi.json",
    },
  ],
};

export default nextConfig;
