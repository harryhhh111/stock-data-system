import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// 开发时代理目标可通过环境变量覆盖。
// 默认按文档双服务器架构：CN 在国内服务器，US 在海外服务器。
const API_PROXY_CN = process.env.VITE_API_PROXY_CN || "http://134.175.237.24:8000";
const API_PROXY_US = process.env.VITE_API_PROXY_US || "http://43.167.190.219:8000";

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
        target: API_PROXY_CN,
        rewrite: (p) => p.replace(/^\/api\/cn/, ""),
      },
      "/api/us": {
        target: API_PROXY_US,
        rewrite: (p) => p.replace(/^\/api\/us/, ""),
        changeOrigin: true,
      },
    },
  },
});
