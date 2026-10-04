import type { NextConfig } from "next";

// The browser only talks to Next.js; /api/* is proxied to FastAPI, so no CORS
// setup is needed and file downloads keep working on the same origin.
const API_URL = process.env.API_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API_URL}/api/:path*` },
      { source: "/api-docs", destination: `${API_URL}/docs` },
      { source: "/openapi.json", destination: `${API_URL}/openapi.json` },
    ];
  },
};

export default nextConfig;
