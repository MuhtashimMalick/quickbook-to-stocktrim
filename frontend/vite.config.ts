/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: the API lives on the same origin as the SPA in production (Caddy). In `npm run dev` proxy it to the stack.
const API = process.env.VITE_API_PROXY ?? "https://caddy";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy: { "/api": { target: API, changeOrigin: true, secure: false } } },
  build: { sourcemap: false, target: "es2022", assetsInlineLimit: 0 },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test/setup.ts"],
    css: false,
    testTimeout: 15000,
  },
});
