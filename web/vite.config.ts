import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Where the dev server forwards `/api` and `/realtime`. The default is the API
// on this machine; `docker-compose.dev.yml` sets it to `http://api:8000`,
// because inside the compose network `127.0.0.1` is the console's own
// container and the proxy would answer its own request.
const API = process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    // 0.0.0.0 so the port is reachable from outside a container. On a host this
    // changes nothing you would notice.
    host: true,
    // Bind mounts do not deliver inotify events reliably across the Docker
    // Desktop boundary on Windows and macOS, and a dev server that never
    // rebuilds looks like a broken build rather than a missed event.
    // `docker-compose.dev.yml` turns this on; on a host it stays off, because
    // polling a node_modules tree is a warm laptop for no reason.
    watch: process.env.VITE_POLL ? { usePolling: true, interval: 300 } : undefined,
    // Dev only. In production Caddy serves `dist/` and proxies the same prefix,
    // so the client's request paths are identical in both — no `VITE_API_URL`
    // to get wrong, and no CORS: the browser only ever talks to one origin.
    proxy: {
      "/api": { target: API, changeOrigin: true },
      // The realtime gateway is at `/realtime`, outside the versioned prefix,
      // so `/api` alone did not cover it and the dev server answered the
      // handshake itself — the socket simply never worked under `npm run dev`.
      // `ws: true` is the half that matters: without it this proxies the HTTP
      // request and drops the upgrade.
      "/realtime": { target: API, changeOrigin: true, ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
