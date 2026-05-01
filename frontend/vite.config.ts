import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          "echarts-core": ["echarts/core", "echarts/renderers"],
          "echarts-charts": ["echarts/charts", "echarts/components"],
          vendor: ["react", "react-dom", "react-router-dom", "@tanstack/react-query", "@tanstack/react-table"],
        },
      },
    },
  },
  server: {
    proxy: {
      "/api/cn": {
        target: "http://localhost:8000",
        rewrite: (p) => p.replace(/^\/api\/cn/, ""),
      },
      "/api/us": {
        target: "http://43.167.190.219:8000",
        rewrite: (p) => p.replace(/^\/api\/us/, ""),
        changeOrigin: true,
      },
    },
  },
});