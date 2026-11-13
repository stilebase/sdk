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

  it("takes the visitor's address from req.ip, or the header you name", async () => {
    await fetch(`${await site()}/listings`, { headers: { ...GPTBOT, "cf-connecting-ip": "198.51.100.9" } });
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("ip")).toBe("127.0.0.1"); // a header anyone can send isn't trusted
    server?.close();
    stilebase = new FakeStilebase();
    await fetch(`${await site({ ipHeader: "cf-connecting-ip" })}/listings`, { headers: { ...GPTBOT, "cf-connecting-ip": "198.51.100.9" } });
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("ip")).toBe("198.51.100.9");
  });

  // Every form that trusts every hop makes req.ip the visitor's first entry; a hop count or named proxies don't.
  const trustAll: unknown[] = [true, () => true, ["0.0.0.0/1", "128.0.0.0/1"], "::/1, 8000::/1"];
  const trustSome: unknown[] = [1, "loopback", "127.0.0.1", false];
  it.each([...trustAll.map((t) => [t, true]), ...trustSome.map((t) => [t, false])])(
    "with trust proxy %s (trusting every hop: %s), takes the last X-Forwarded-For entry, warning only when it trusts every hop",
    async (trust, everyHop) => {
      const warnings: string[] = [];
      const app = express();
      app.set("trust proxy", trust);
      app.use(expressGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, onError: (e) => warnings.push(e.message) }));
      app.get("/listings", (req, res) => {
        res.send(req.ip ?? "");
      });
      server = app.listen(0);
      await new Promise((resolve) => server!.once("listening", resolve));
      const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
      for (let i = 0; i < 2; i++) await fetch(`${base}/listings`, { headers: { ...GPTBOT, "x-forwarded-for": "20.15.240.70, 198.51.100.9" } });
      const ips = stilebase.to("/v1/x402/requirements").map((c) => c.query.get("ip"));
      // Named proxies that don't include 127.0.0.1 (false) trust no hop: the socket's own address.
      expect(ips[0]).toBe(trust === false ? "127.0.0.1" : "198.51.100.9"); // never the spoofed 20.15.240.70
      expect(warnings.filter((w) => w.includes("trust proxy"))).toHaveLength(everyHop ? 1 : 0);
    },
  );

  it("keeps only the exact free path free, however another page is spelt (express.static)", async () => {
    const { mkdtempSync, mkdirSync, rmSync, writeFileSync } = await import("node:fs");
    const { tmpdir } = await import("node:os");
    const { join } = await import("node:path");
    const root = mkdtempSync(join(tmpdir(), "gate-static-"));
    try {
      mkdirSync(join(root, "listings"));
      writeFileSync(join(root, "listings", "1.html"), "the listing");
      writeFileSync(join(root, "robots.txt"), "the robots file");
      writeFileSync(join(root, "index.html"), "the home page");
      writeFileSync(join(root, "listings", "index.html"), "the listings");
      const app = express();
      // "/" is free too: a spelling that reads as "/" to the gate but reaches a listing in Express would be served free.
      app.use(expressGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, free: ["/robots.txt", "/"] }));
      app.use(express.static(root));
      server = app.listen(0);
      await new Promise((resolve) => server!.once("listening", resolve));
      const port = (server.address() as AddressInfo).port;
      const { request: raw } = await import("node:http");
      const get = (path: string) =>
        new Promise<number>((resolve, reject) => {
          const req = raw({ host: "127.0.0.1", port, path, headers: GPTBOT }, (res) => (res.resume(), resolve(res.statusCode ?? 0)));
          req.on("error", reject);
          req.end();
        });
      expect(await get("/robots.txt")).toBe(200);
      const spellings = [
        "/listings/1.html",
        "/robots.txt/../listings/1.html",
        "/robots.txt%2F..%2Flistings/1.html",
        "/%2F/listings/1.html",
        "/x//../listings/1.html",
        "http://h/robots.txt/../listings/1.html",
        "http://h/x//../listings/1.html",
        "http://x%2Flistings%2F1.html?",
        "javascript://listings/1.html",
        "JAVASCRIPT://listings/1.html",
        "javascript://listings/",
        "JAVASCRIPT://listings/",
        "ftp://h/robots.txt/../listings/1.html",
        "http://a@x/robots.txt/../listings/1.html",
      ];
      for (const path of spellings) expect([path, await get(path)]).not.toEqual([path, 200]); // never the listing, unpaid
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("never hands the request to the routes when the gate's own answer can't be sent", async () => {
    const { gate } = await import("../src/express.js");
    const handler = gate({ key: KEY, apiUrl: API, fetch: stilebase.fetch });
    const passed: unknown[] = [];
    const res = {
      headersSent: false,
      status() {
        return this;
      },
      set() {
        throw new Error("ERR_INVALID_CHAR");
      },
      setHeader() {},
      send() {},
    };
    const req = { method: "GET", protocol: "https", originalUrl: "/listings", get: (name: string) => ({ "user-agent": GPTBOT["user-agent"], host: DOMAIN })[name.toLowerCase()] };
    await new Promise<void>((resolve) => handler(req, res, (error?: unknown) => (passed.push(error), resolve())));
    expect(passed).toHaveLength(1);
    expect(passed[0]).toBeInstanceOf(Error); // an error, never next() with no argument (which serves the page)
  });

  it("a clientIp or onError that throws doesn't break the request", async () => {
    const app = express();
    app.set("trust proxy", true);
    app.use(expressGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, onError: () => { throw new Error("boom"); } }));
    app.get("/listings", (_req, res) => {
      res.send("the listings");
    });
    server = app.listen(0);
    await new Promise((resolve) => server!.once("listening", resolve));
    const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    expect((await fetch(`${base}/listings`, { headers: BROWSER })).status).toBe(200);
    server.close();
    server = undefined;
    const throwing = express();
    throwing.use(expressGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, clientIp: () => { throw new Error("boom"); } }));
    throwing.get("/listings", (_req, res) => {
      res.send("the listings");
    });
    server = throwing.listen(0);
    await new Promise((resolve) => server!.once("listening", resolve));
    const other = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    expect((await fetch(`${other}/listings`, { headers: GPTBOT })).status).toBe(402);
  });

  it("takes the last X-Forwarded-For entry, the one the proxy added, not one the visitor wrote", async () => {
    await fetch(`${await site({ ipHeader: "x-forwarded-for" })}/listings`, { headers: { ...GPTBOT, "x-forwarded-for": "20.15.240.70, 198.51.100.9" } });
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("ip")).toBe("198.51.100.9");
  });
});

describe("express: the review's cases (cycle 1)", () => {
  let server: ReturnType<ReturnType<typeof express>["listen"]> | undefined;
  afterEach(() => server?.close());

  it("a Host header with ? or # doesn't change the path the gate reads", async () => {
    const app = express();
    app.use(expressGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, free: ["/premium"] }));
    // A forged Host ("a/premium?") must not make /premium/report read as the free /premium.
    app.get("/premium/report", (_req, res) => {
      res.send("the report");
    });
    server = app.listen(0);
    await new Promise((resolve) => server!.once("listening", resolve));
    const { request: raw } = await import("node:http");
    const port = (server.address() as AddressInfo).port;
    const status = await new Promise<number>((resolve, reject) => {
      const req = raw({ host: "127.0.0.1", port, path: "/premium/report", headers: { host: "a/premium?", ...GPTBOT } }, (res) => {
        res.resume();
        resolve(res.statusCode ?? 0);
      });
      req.on("error", reject);
      req.end();
    });
    expect(status).toBe(402);
  });
});

describe("fetch (Workers, Hono, Bun, Deno)", () => {
  // A Worker's handler: (request, env, ctx).
  const page = (_request: Request, _env?: unknown, _ctx?: unknown) => new Response("the listings", { headers: { "content-type": "text/plain" } });

  it("a raw request URL with a forged Host (as Deno builds it) is never free", async () => {
    const handler = withGate(page, { key: KEY, apiUrl: API, fetch: stilebase.fetch, free: ["/", "/healthz"] });
    for (const url of ["http://x?/premium/report", "http://x#/premium/report", `http://${DOMAIN}?/premium/report`]) {
      const raw = { method: "GET", url, headers: new Headers(GPTBOT) } as unknown as Request; // not normalized, as Deno hands it over
      expect([url, (await handler(raw)).status]).toEqual([url, 402]);
    }
  });

  it("a clientIp giving an object or a promise is no address, never an unhandled rejection; agents still pay", async () => {
    type Info = { remoteAddr: { hostname: string } };
    const deno = (_request: Request, _info: Info) => new Response("the listings");
    const info: Info = { remoteAddr: { hostname: "198.51.100.9" } };
    // The documented Deno form type-checks: clientIp gets the handler's own arguments.
    const typed = withGate(deno, { key: KEY, apiUrl: API, fetch: stilebase.fetch, clientIp: (_request, i) => i.remoteAddr.hostname });
    expect((await typed(new Request(`https://${DOMAIN}/listings`, { headers: GPTBOT }), info)).status).toBe(402);
    expect(stilebase.to("/v1/x402/requirements").at(-1)?.query.get("ip")).toBe("198.51.100.9");
    const unhandled: unknown[] = [];
    const onUnhandled = (reason: unknown) => unhandled.push(reason);
    process.on("unhandledRejection", onUnhandled);
    try {
      for (const clientIp of [() => info.remoteAddr, () => Promise.reject(new Error("boom")), () => Promise.resolve("1.2.3.4")]) {
        const handler = withGate(deno, { key: KEY, apiUrl: API, fetch: stilebase.fetch, clientIp: clientIp as unknown as () => string });
        expect((await handler(new Request(`https://${DOMAIN}/listings`, { headers: GPTBOT }), info)).status).toBe(402);
        expect((await handler(new Request(`https://${DOMAIN}/listings`, { headers: BROWSER }), info)).status).toBe(200);
      }
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(unhandled).toEqual([]);
    } finally {
      process.off("unhandledRejection", onUnhandled);
    }
  });

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
