import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 開發時把 /api 與 /auth（登入）轉給查詢服務（uv run tmig serve，預設 8000 埠）
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000", "/auth": "http://127.0.0.1:8000" },
  },
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 2000,
  },
});
