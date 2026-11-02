import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 前端通过 /api 访问后端；MCP 与 OAuth 端点（/mcp、/oauth、/.well-known）也代理过去，开发时外部客户端可以连 5173。
// 生产环境由后端单端口同时提供，不经过 Vite。
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: Object.fromEntries(["/api", "/mcp", "/oauth/authorize", "/oauth/token", "/oauth/revoke", "/oauth/register", "/.well-known"].map((path) =>
      [path, { target: process.env.AVELIS_API ?? "http://127.0.0.1:8000", changeOrigin: false }])),
  },
});
