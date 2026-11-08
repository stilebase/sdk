import type { AddressInfo } from "node:net";

import express from "express";
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { gate as expressGate } from "../src/express.js";
import { withGate } from "../src/fetch.js";
import { gate as nextGate } from "../src/next.js";
import { API, BROWSER, DOMAIN, FakeStilebase, GPTBOT, KEY, REQUIRED } from "./fake-stilebase.js";

const PAID = { ...GPTBOT, "payment-signature": "eyJ4NDAyVmVyc2lvbiI6Mn0=" };
let stilebase: FakeStilebase;

beforeEach(() => {
  stilebase = new FakeStilebase();
});

describe("express", () => {
  let server: ReturnType<ReturnType<typeof express>["listen"]> | undefined;
  afterEach(() => server?.close());

  async function site(options: Parameters<typeof expressGate>[0] = {}) {
    const app = express();
    app.use(expressGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, ...options }));
    app.get("/listings", (_req, res) => {
      res.send("the listings");
    });
    server = app.listen(0);
    await new Promise((resolve) => server!.once("listening", resolve));
    return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  }

  it("answers agents 402, serves people, and serves paid requests with PAYMENT-RESPONSE", async () => {
    const base = await site();
    const asked = await fetch(`${base}/listings`, { headers: GPTBOT });
    expect(asked.status).toBe(402);
    expect(JSON.parse(atob(asked.headers.get("payment-required")!))).toEqual(REQUIRED);
    const person = await fetch(`${base}/listings`, { headers: BROWSER });
    expect([person.status, await person.text()]).toEqual([200, "the listings"]);
    const paid = await fetch(`${base}/listings`, { headers: PAID });
    expect([paid.status, await paid.text(), paid.headers.get("payment-response")]).toEqual([200, "the listings", "eyJzdWNjZXNzIjp0cnVlfQ=="]);
  });

  it("takes the visitor's address from req.ip, or the header you name", async () => {
    await fetch(`${await site()}/listings`, { headers: { ...GPTBOT, "cf-connecting-ip": "198.51.100.9" } });
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("ip")).toBe("127.0.0.1"); // a header anyone can send isn't trusted
    server?.close();
    stilebase = new FakeStilebase();
    await fetch(`${await site({ ipHeader: "cf-connecting-ip" })}/listings`, { headers: { ...GPTBOT, "cf-connecting-ip": "198.51.100.9" } });
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("ip")).toBe("198.51.100.9");
  });

  it("with trust proxy true (req.ip is then the visitor's first entry), takes the last X-Forwarded-For entry and warns", async () => {
    const warnings: string[] = [];
    const app = express();
    app.set("trust proxy", true);
    app.use(expressGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, onError: (e) => warnings.push(e.message) }));
    app.get("/listings", (req, res) => {
      res.send(req.ip ?? "");
    });
    server = app.listen(0);
    await new Promise((resolve) => server!.once("listening", resolve));
    const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    await fetch(`${base}/listings`, { headers: { ...GPTBOT, "x-forwarded-for": "20.15.240.70, 198.51.100.9" } });
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("ip")).toBe("198.51.100.9"); // not the spoofed 20.15.240.70
    expect(warnings.filter((w) => w.includes("trust proxy"))).toHaveLength(1);
  });

  it("takes the last X-Forwarded-For entry, the one the proxy added, not one the visitor wrote", async () => {
    await fetch(`${await site({ ipHeader: "x-forwarded-for" })}/listings`, { headers: { ...GPTBOT, "x-forwarded-for": "20.15.240.70, 198.51.100.9" } });
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("ip")).toBe("198.51.100.9");
  });
});

describe("express: the review's cases (cycle 1)", () => {
  let server: ReturnType<ReturnType<typeof express>["listen"]> | undefined;
  afterEach(() => server?.close());

  it("a Host header with ? or # doesn't move the path out of reach of `paths`", async () => {
    const app = express();
    app.use(expressGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, paths: ["/premium/"] }));
    app.get("/premium/report", (_req, res) => {
      res.send("the report");
    });
    server = app.listen(0);
    await new Promise((resolve) => server!.once("listening", resolve));
    const { request: raw } = await import("node:http");
    const port = (server.address() as AddressInfo).port;
    const status = await new Promise<number>((resolve, reject) => {
      const req = raw({ host: "127.0.0.1", port, path: "/premium/report", headers: { host: "example.com?", ...GPTBOT } }, (res) => {
        res.resume();
        resolve(res.statusCode ?? 0);
      });
      req.on("error", reject);
      req.end();
    });
    expect(status).toBe(402);
  });
});

describe("next", () => {
  const middleware = () => nextGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch });

  it("answers agents 402 and lets people through to the page", async () => {
    const asked = await middleware()(new NextRequest(`https://${DOMAIN}/listings`, { headers: GPTBOT }));
    expect(asked.status).toBe(402);
    expect(asked.headers.get("payment-required")).toBeTruthy();
    const person = await middleware()(new NextRequest(`https://${DOMAIN}/listings`, { headers: BROWSER }));
    expect(person.headers.get("x-middleware-next")).toBe("1"); // NextResponse.next(): on to the page
  });

  it("passes a paid request on with PAYMENT-RESPONSE, and keeps reports alive with waitUntil", async () => {
    const tasks: Promise<unknown>[] = [];
    const event = { waitUntil: (task: Promise<unknown>) => tasks.push(task) } as unknown as Parameters<ReturnType<typeof nextGate>>[1];
    const paid = await middleware()(new NextRequest(`https://${DOMAIN}/listings`, { headers: PAID }), event);
    expect([paid.headers.get("x-middleware-next"), paid.headers.get("payment-response")]).toEqual(["1", "eyJzdWNjZXNzIjp0cnVlfQ=="]);
    await middleware()(new NextRequest(`https://${DOMAIN}/listings`, { headers: GPTBOT }), event);
    expect(tasks.length).toBeGreaterThan(0);
    await Promise.all(tasks);
    expect(stilebase.to("/v1/x402/visits")).toHaveLength(1);
  });
});

describe("fetch (Workers, Hono, Bun, Deno)", () => {
  // A Worker's handler: (request, env, ctx).
  const page = (_request: Request, _env?: unknown, _ctx?: unknown) => new Response("the listings", { headers: { "content-type": "text/plain" } });

  it("takes the key from the Worker's env, and adds PAYMENT-RESPONSE to a paid response", async () => {
    const handler = withGate(page, { apiUrl: API, fetch: stilebase.fetch });
    const tasks: Promise<unknown>[] = [];
    const ctx = { waitUntil: (task: Promise<unknown>) => tasks.push(task) };
    const env = { STILEBASE_KEY: KEY };
    expect((await handler(new Request(`https://${DOMAIN}/listings`, { headers: GPTBOT }), env, ctx)).status).toBe(402);
    expect(tasks).toHaveLength(1); // the visit report, kept alive past the response
    const paid = await handler(new Request(`https://${DOMAIN}/listings`, { headers: PAID }), env, ctx);
    expect([paid.status, await paid.text(), paid.headers.get("payment-response")]).toEqual([200, "the listings", "eyJzdWNjZXNzIjp0cnVlfQ=="]);
  });

  it("takes STILEBASE_API_URL from the Worker's env too", async () => {
    const other = new FakeStilebase();
    const seen: string[] = [];
    const handler = withGate(page, { fetch: (input, init) => (seen.push(String(input)), other.fetch(input, init)) });
    await handler(new Request(`https://${DOMAIN}/listings`, { headers: GPTBOT }), { STILEBASE_KEY: KEY, STILEBASE_API_URL: API }, undefined);
    expect(seen[0]?.startsWith(`${API}/v1/x402/sites/`)).toBe(true);
  });

  it("reads Cloudflare's address and edge facts only on Cloudflare", async () => {
    const handler = withGate(page, { key: KEY, apiUrl: API, fetch: stilebase.fetch });
    const onCloudflare = Object.assign(new Request(`https://${DOMAIN}/listings`, { headers: { ...GPTBOT, "cf-connecting-ip": "198.51.100.9" } }), {
      cf: { httpProtocol: "HTTP/2", asn: 13335 },
    });
    await handler(onCloudflare);
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("ip")).toBe("198.51.100.9");
    await withGate(page, { key: KEY, apiUrl: API, fetch: stilebase.fetch })(new Request(`https://${DOMAIN}/other`, { headers: { ...GPTBOT, "cf-connecting-ip": "198.51.100.9" } }));
    expect(stilebase.to("/v1/x402/requirements")[1]?.query.get("ip")).toBe("");
  });
});
