import basicSsl from "@vitejs/plugin-basic-ssl";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const apiTarget = process.env.GAMEFORGE_WEB_API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), basicSsl()],
  server: {
    hmr: process.env.GAMEFORGE_WEB_HMR === "off" ? false : undefined,
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: false,
        secure: false,
        ws: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    clearMocks: true,
    include: ["src/**/*.test.{ts,tsx}", "scripts/**/*.test.ts"],
  },
});
