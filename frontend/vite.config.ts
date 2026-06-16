import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// 开发时代理目标可通过环境变量覆盖，避免每个人本地 IP 不同还要改仓库文件
const API_PROXY_CN = process.env.VITE_API_PROXY_CN || "http://localhost:8000";
const API_PROXY_US = process.env.VITE_API_PROXY_US || "http://localhost:8000";

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
