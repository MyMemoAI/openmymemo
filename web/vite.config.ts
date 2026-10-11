import { fileURLToPath, URL } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

// The built panel ships inside the Python package so `openmymemo serve` needs no Node at runtime.
export default defineConfig({
  plugins: [vue(), tailwindcss()],
  build: {
    outDir: fileURLToPath(new URL("../src/openmymemo/static", import.meta.url)),
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // `openmymemo serve --port 8787 --no-open` must be running; the proxy rewrites Host/Origin for its guard.
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8787",
        changeOrigin: true,
        headers: { origin: "http://127.0.0.1:8787" },
      },
    },
  },
});
