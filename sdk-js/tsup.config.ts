import { readFileSync } from "node:fs";

import { defineConfig } from "tsup";

const { version } = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf8")) as { version: string };

// One entry per import path (package.json "exports"), as ESM and CommonJS, each with its type declarations.
export default defineConfig({
  entry: {
    index: "src/index.ts",
    "gate/index": "src/gate.ts",
    "gate/express": "src/express.ts",
    "gate/next": "src/next.ts",
    "gate/fetch": "src/fetch.ts",
  },
  format: ["esm", "cjs"],
  dts: true,
  clean: true,
  target: "es2022",
  platform: "neutral", // runs on Node, Workers, Deno, Bun and Next's edge runtime: no Node built-ins
  external: ["next", "next/server.js"],
  define: { __GATE_VERSION__: JSON.stringify(version) },
  sourcemap: true,
});
