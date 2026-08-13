import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Where the dev server forwards `/api` and `/realtime`. Default is the API on
// this machine; `docker-compose.dev.yml` sets it to `http://api:8000`, because
// inside the compose network `127.0.0.1` is this container.
const API = process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    // 5174, not 5173. The admin console owns 5173, and the two run side by side
    // during development — ADR-0002 keeps them on separate origins, and a port
    // collision would be the first thing to break that.
    port: 5174,
    strictPort: true,
    host: true,
    watch: process.env.VITE_POLL ? { usePolling: true, interval: 300 } : undefined,
    // Same-origin, which is the whole reason there is no CORS middleware in the
    // API and the reason the refresh cookie can be `SameSite=Strict`
    // (ADR-0002 decision 3). Caddy proxies the identical prefix in production,
    // so request paths are the same in both and there is no base-URL variable
    // to get wrong.
    proxy: {
      "/api": { target: API, changeOrigin: true },
      "/realtime": { target: API, changeOrigin: true, ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
