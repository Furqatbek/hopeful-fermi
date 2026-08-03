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
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
