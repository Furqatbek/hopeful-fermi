import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    // Dev only. In production Caddy serves `dist/` and proxies the same prefix,
    // so the client's request paths are identical in both — no `VITE_API_URL`
    // to get wrong, and no CORS: the browser only ever talks to one origin.
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      // The realtime gateway is at `/realtime`, outside the versioned prefix,
      // so `/api` alone did not cover it and the dev server answered the
      // handshake itself — the socket simply never worked under `npm run dev`.
      // `ws: true` is the half that matters: without it this proxies the HTTP
      // request and drops the upgrade.
      "/realtime": { target: "http://127.0.0.1:8000", changeOrigin: true, ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
