import type { NextConfig } from "next";

const config: NextConfig = {
  output: "standalone", // small self-contained server for the Docker image
  poweredByHeader: false,
};

export default config;
