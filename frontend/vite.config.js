import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev (`npm run dev`) proxy the API/WebSocket to a locally running backend.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
