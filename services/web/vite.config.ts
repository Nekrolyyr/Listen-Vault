import { defineConfig } from "vite";

// In development, run the api locally and proxy it (headers are added by the dev proxy below,
// standing in for the login proxy). Production builds are served by the api itself.
const devUser = process.env.LISTEN_USER ?? "doctor";
const devSecret = process.env.LISTEN_PROXY_SECRET ?? "dev-secret";

export default defineConfig({
  build: { target: "es2022", sourcemap: false, chunkSizeWarningLimit: 1500 },
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        headers: { "Remote-User": devUser, "X-Listen-Proxy": devSecret },
      },
    },
  },
});
