import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by the core under /app in production (core/api.py); in development
// Vite proxies API calls of the *own* instance to a running core. Other
// household members are reached by their absolute URL (CORS on loopback).
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8420", "/healthz": "http://127.0.0.1:8420" },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
