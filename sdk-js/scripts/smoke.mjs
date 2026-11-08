// The package as users get it: `npm pack`, installed into an empty project, every entry imported (ESM and CommonJS)
// and its types checked. Run after `npm run build`. SMOKE_TARBALL=<path> checks that tarball instead of packing one
// (the release checks the exact file it publishes).

import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const work = mkdtempSync(join(tmpdir(), "stilebase-sdk-smoke-"));
const npm = (args, cwd) => execFileSync("npm", args, { cwd, stdio: ["ignore", "pipe", "inherit"], shell: process.platform === "win32" }).toString();

try {
  const tarball = process.env.SMOKE_TARBALL
    ? resolve(process.env.SMOKE_TARBALL)
    : join(work, JSON.parse(npm(["pack", "--json", "--pack-destination", work], root))[0].filename);
  writeFileSync(join(work, "package.json"), JSON.stringify({ name: "smoke", private: true, type: "module" }));
  // --ignore-scripts: the smoke test's own third-party installs run no scripts.
  npm(["install", "--no-audit", "--no-fund", "--ignore-scripts", tarball, "express@5", "next@15", "react@19", "react-dom@19", "typescript@5.6", "@types/express@5", "@types/node@20"], work);

  writeFileSync(
    join(work, "esm.mjs"),
    `import { createGate, VERSION } from "@stilebase/sdk/gate";
import * as sdk from "@stilebase/sdk";
if (sdk.gate.createGate !== createGate || sdk.VERSION !== VERSION) throw new Error("sdk.gate isn't the gate");
import { gate as express } from "@stilebase/sdk/gate/express";
import { gate as next } from "@stilebase/sdk/gate/next";
import { withGate } from "@stilebase/sdk/gate/fetch";
for (const f of [createGate, express, next, withGate]) if (typeof f !== "function") throw new Error("missing export");
if (!/^\\d+\\.\\d+\\.\\d+/.test(VERSION)) throw new Error("no version");
const decision = await createGate({ key: "" , onError() {} }).check({ method: "GET", url: "https://a.example/", headers: new Headers() });
if (decision.action !== "pass") throw new Error("a keyless gate must pass");
console.log("esm ok", VERSION);`,
  );
  writeFileSync(
    join(work, "cjs.cjs"),
    `const { createGate } = require("@stilebase/sdk/gate");
const { gate } = require("@stilebase/sdk/gate/express");
const { withGate } = require("@stilebase/sdk/gate/fetch");
for (const f of [createGate, gate, withGate]) if (typeof f !== "function") throw new Error("missing export");
console.log("cjs ok");`,
  );
  writeFileSync(
    join(work, "types.ts"),
    `import express from "express";
import { gate } from "@stilebase/sdk/gate/express";
import { gate as nextGate } from "@stilebase/sdk/gate/next";
import { withGate } from "@stilebase/sdk/gate/fetch";
import { createGate, type GateDecision } from "@stilebase/sdk/gate";
const app = express();
app.use(gate({ paths: ["/listings/"], ipHeader: "cf-connecting-ip" }));
export const middleware = nextGate({ mode: "test" });
export default { fetch: withGate((request: Request) => new Response(request.url)) };
export const decided: Promise<GateDecision> = createGate().check({ method: "GET", url: "https://a.example/", headers: new Headers() });`,
  );
  writeFileSync(
    join(work, "tsconfig.json"),
    JSON.stringify({ compilerOptions: { strict: true, module: "NodeNext", moduleResolution: "NodeNext", target: "ES2022", noEmit: true, skipLibCheck: true, lib: ["ES2022", "DOM"], types: ["node"] }, files: ["types.ts"] }),
  );
  for (const file of ["esm.mjs", "cjs.cjs"]) console.log(execFileSync("node", [file], { cwd: work, stdio: ["ignore", "pipe", "inherit"] }).toString().trim());
  execFileSync(process.platform === "win32" ? "npx.cmd" : "npx", ["tsc", "-p", "."], { cwd: work, stdio: "inherit", shell: process.platform === "win32" });
  console.log("types ok");
} finally {
  rmSync(work, { recursive: true, force: true });
}
