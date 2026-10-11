/// <reference types="vitest/config" />
import { fileURLToPath, URL } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

// The built panel ships inside the Python package so `openmymemo serve` needs no Node at runtime.
// In development the API goes to `openmymemo serve`, on 8787 unless OPENMYMEMO_PORT says otherwise.
const core = `http://127.0.0.1:${process.env.OPENMYMEMO_PORT ?? "8787"}`;

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  build: {
    outDir: fileURLToPath(new URL("../src/openmymemo/static", import.meta.url)),
    emptyOutDir: true,
  },
  test: {
    environment: "happy-dom",
  },
  server: {
    port: 5173,
    // `openmymemo serve --no-open` must be running; the proxy rewrites Host/Origin for its guard.
    proxy: {
      "/api": {
        target: core,
        changeOrigin: true,
        headers: { origin: core },
      },
    },
  },
});
