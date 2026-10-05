import type { NextConfig } from "next";

// Where the Django API is reachable from the Next.js server. Rewrites are resolved at build time
// for standalone output, so Docker images are built with the compose service URL.
const apiUrl = (process.env.REVIEWBOT_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${apiUrl}/api/:path*` },
      { source: "/webhooks/:path*", destination: `${apiUrl}/webhooks/:path*` },
      { source: "/healthz", destination: `${apiUrl}/healthz` },
      { source: "/readyz", destination: `${apiUrl}/readyz` },
    ];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "same-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
