import withSerwistInit from "@serwist/next";

/** @type {import('next').NextConfig} */
const nextConfig = {
  eslint: {
    ignoreDuringBuilds: true,
  },

  typescript: {
    ignoreBuildErrors: false,
  },

  experimental: {
    workerThreads: false,
    cpus: 1,
  },
};

const withSerwist = withSerwistInit({
  swSrc: "src/app/sw.ts",
  swDest: "public/sw.js",

  // Don't run the service worker during local development.
  // This prevents stale PWA caches while developing.
  disable: process.env.NODE_ENV === "development",

  // Automatically register the generated service worker.
  register: true,

  // Reload the application when it comes back online.
  reloadOnOnline: true,
});

export default withSerwist(nextConfig);
