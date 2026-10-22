/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The browser talks to FastAPI directly (see VITE_API_URL in src/api.ts), not
// through a Vite proxy. That keeps the dev setup identical to production and
// means no dev-server proxy can accidentally buffer our SSE stream.
export default defineConfig({
  plugins: [react()],
  server: { port: 3100 },
  test: { environment: "node" },
});
