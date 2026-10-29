import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 前端只通过 /api 访问后端；开发时由 Vite 代理到本机 API（默认 127.0.0.1:8000）
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: process.env.AVELIS_API ?? "http://127.0.0.1:8000", changeOrigin: false } },
  },
});
