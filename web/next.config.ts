import path from "node:path";

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The repo root holds the Python pipeline; this app is self-contained here.
  turbopack: { root: path.join(__dirname) },
};

export default nextConfig;
