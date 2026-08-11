import {defineConfig} from "vite";
import react from "@vitejs/plugin-react";
import {resolve} from "node:path";

export default defineConfig({
  base: "/static/",
  plugins: [react()],
  build: {
    outDir: resolve(import.meta.dirname, "../src/app/web"),
    emptyOutDir: true,
  },
});
