import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the Vite server proxies /api to the backend, so the backend needs no CORS setup.
// `npm run build` writes static files to dist/, which FastAPI serves at "/".
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
