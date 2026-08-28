import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import { tanstackRouter } from "@tanstack/router-plugin/vite";
import react from "@vitejs/plugin-react";
import { visualizer } from "rollup-plugin-visualizer";
import { defineConfig } from "vite";

// https://vite.dev/config/
export default defineConfig(({ command }) => ({
  // In production Django + WhiteNoise serve the built assets under /static/
  // (index.html is served for every non-API path); in dev Vite serves "/".
  base: command === "build" ? "/static/" : "/",
  plugins: [
    // Must run before react(): generates src/routeTree.gen.ts from src/routes/
    // and splits every route file into its own lazily loaded chunk.
    tanstackRouter({
      target: "react",
      autoCodeSplitting: true,
      quoteStyle: "double",
      semicolons: true,
    }),
    react(),
    tailwindcss(),
    // Bundle analysis: writes frontend/stats.html (git-ignored) on every build.
    visualizer({ filename: "stats.html", gzipSize: true, brotliSize: true }),
  ],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    // Forward API calls to the Django dev server (no path rewrite:
    // /api/health -> http://localhost:8000/api/health). API_PROXY_TARGET points a second Vite
    // at another Django instance, e.g. one running with CELERY_EAGER=false on port 8001.
    proxy: {
      "/api": {
        target: process.env.API_PROXY_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
      },
      // Uploaded files (`<img src>`, `<video src>`, download links) are served by Django too.
      "/media": {
        target: process.env.API_PROXY_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    // Vite 8 bundles with rolldown; `rollupOptions`/`manualChunks`/`advancedChunks`
    // are deprecated aliases, `codeSplitting.groups` is the current API.
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              // Stable framework chunk: app deploys don't invalidate it as long
              // as these dependency versions stay the same.
              name: "vendor",
              test: /node_modules[\\/](react|react-dom|scheduler|@tanstack)[\\/]/,
              priority: 10,
            },
          ],
        },
      },
    },
  },
}));
