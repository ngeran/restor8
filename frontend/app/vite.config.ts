import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev parity with prod: in-cluster the Ingress routes restor8.home/api/*
// and /ws to the gateway; locally `just run`-style dev proxies the same
// paths to a gateway port-forward (kubectl -n restor8 port-forward
// svc/restor8-gateway 18086:8080).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": "http://localhost:18086",
      "/ws": { target: "ws://localhost:18086", ws: true },
    },
  },
});
