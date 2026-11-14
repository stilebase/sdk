import type { AddressInfo } from "node:net";

import express from "express";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { gate as expressGate } from "../src/express.js";
import { withGate } from "../src/fetch.js";
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

  it("takes the visitor's address from req.ip, or the header you name (its last entry)", async () => {
    await fetch(`${await site()}/listings`, { headers: { ...GPTBOT, "cf-connecting-ip": "198.51.100.9" } });
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("ip")).toBe("127.0.0.1"); // a header anyone can send isn't trusted
    server?.close();
    stilebase = new FakeStilebase();
    await fetch(`${await site({ ipHeader: "x-forwarded-for" })}/listings`, { headers: { ...GPTBOT, "x-forwarded-for": "20.15.240.70, 198.51.100.9" } });
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("ip")).toBe("198.51.100.9");
  });

  it("names an absolute-form target by its path, as Express routes it", async () => {
    const { request: raw } = await import("node:http");
    const base = new URL(await site());
    await new Promise<void>((resolve, reject) => {
      const req = raw({ host: base.hostname, port: base.port, path: "http://x/listings?a=1", headers: GPTBOT }, (res) => {
        res.resume();
        res.on("end", resolve);
      });
      req.on("error", reject);
      req.end();
    });
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("url")).toBe(`https://${DOMAIN}/listings?a=1`);
  });

  it("names an absolute-form target new URL can't read (a port too large) by its path, as Express routes it", async () => {
    const { request: raw } = await import("node:http");
    const base = new URL(await site());
    await new Promise<void>((resolve, reject) => {
      const req = raw({ host: base.hostname, port: base.port, path: "http://host:99999/listings", headers: GPTBOT }, (res) => {
        res.resume();
        res.on("end", resolve);
      });
      req.on("error", reject);
      req.end();
    });
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("url")).toBe(`https://${DOMAIN}/listings`);
  });

  it("names a path starting with two slashes as Express routes it, the whole path", async () => {
    const { request: raw } = await import("node:http");
    const base = new URL(await site());
    await new Promise<void>((resolve, reject) => {
      const req = raw({ host: base.hostname, port: base.port, path: "//premium/article.html", headers: GPTBOT }, (res) => {
        res.resume();
        res.on("end", resolve);
      });
      req.on("error", reject);
      req.end();
    });
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("url")).toBe(`https://${DOMAIN}//premium/article.html`);
  });

  it("handles a clientIp that returns a promise: no address, never an unhandled rejection", async () => {
    const base = await site({ clientIp: (() => Promise.reject(new Error("lookup failed"))) as unknown as () => string });
    expect((await fetch(`${base}/listings`, { headers: GPTBOT })).status).toBe(402);
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("ip")).toBe("");
  });

  it("names the page by the gateway's domain and the path Express routes", async () => {
    await fetch(`${await site()}/listings?city=Haifa`, { headers: GPTBOT });
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("url")).toBe(`https://${DOMAIN}/listings?city=Haifa`);
  });
});

describe("fetch (Workers, Hono, Bun, Deno)", () => {
  const handler = (_request: Request, _env?: unknown, _ctx?: unknown) => new Response("the page");
  const env = { STILEBASE_KEY: KEY, STILEBASE_API_URL: API };

  it("on Workers: the key from env, the address from CF-Connecting-IP, reports kept alive with waitUntil", async () => {
    const waits: Promise<unknown>[] = [];
    const ctx = { waitUntil: (task: Promise<unknown>) => waits.push(task) };
    const worker = withGate(handler, { fetch: stilebase.fetch });
    const request = new Request(`https://${DOMAIN}/listings`, { headers: { ...GPTBOT, "cf-connecting-ip": "20.15.240.70" } });
    Object.assign(request, { cf: { httpProtocol: "HTTP/2", asn: 8075 } });
    const asked = await worker(request, env, ctx);
    expect(asked.status).toBe(402);
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("ip")).toBe("20.15.240.70");
    await Promise.all(waits);
    expect(stilebase.to("/v1/x402/visits")).toHaveLength(1);
  });

  it("serves people, and paid requests with PAYMENT-RESPONSE", async () => {
    const worker = withGate(handler, { fetch: stilebase.fetch });
    const person = await worker(new Request(`https://${DOMAIN}/listings`, { headers: BROWSER }), env, {});
    expect([person.status, await person.text()]).toEqual([200, "the page"]);
    const paid = await worker(new Request(`https://${DOMAIN}/listings`, { headers: PAID }), env, {});
    expect([paid.status, await paid.text(), paid.headers.get("payment-response")]).toEqual([200, "the page", "eyJzdWNjZXNzIjp0cnVlfQ=="]);
  });

  it("outside Cloudflare, reads no address unless told where", async () => {
    const worker = withGate(handler, { fetch: stilebase.fetch, key: KEY, apiUrl: API });
    await worker(new Request(`https://${DOMAIN}/x`, { headers: { ...GPTBOT, "cf-connecting-ip": "20.15.240.70" } }));
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("ip")).toBe("");
  });
});
