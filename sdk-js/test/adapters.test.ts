// The adapters: the same gate, through Express and the Fetch API, end to end against a fake Stilebase.

import type { AddressInfo } from "node:net";

import express from "express";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { gate as expressGate } from "../src/express.js";
import { withGate } from "../src/fetch.js";
import { API, BROWSER, CAPTURES, DECIDE, FakeStilebase, GPTBOT, KEY, PAID, SETTLE } from "./fake-stilebase.js";

let stilebase: FakeStilebase;
const BIG = "x".repeat(2000);

beforeEach(() => {
  stilebase = new FakeStilebase();
});

const holding = (max = 1000) => () => ({ status: 200, body: { action: "pass", settle_after: { max_bytes: max } } });

describe("express", () => {
  let server: ReturnType<ReturnType<typeof express>["listen"]> | undefined;
  afterEach(() => server?.close());

  async function site(options: Parameters<typeof expressGate>[0] = {}) {
    const app = express();
    app.use(expressGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, onError: () => undefined, ...options }));
    app.get("/listings", (_req, res) => {
      res.set("x-site", "yes").send("the listings");
    });
    app.get("/stream", (_req, res) => {
      res.status(200);
      res.write("part one, ");
      res.end("part two");
    });
    app.get("/big", (_req, res) => {
      res.send(BIG);
    });
    app.get("/broken", (_req, res) => {
      res.status(500).send("oops");
    });
    app.get("/callback", (_req, res) => {
      res.write("done");
      res.end(() => undefined);
    });
    server = app.listen(0);
    await new Promise((resolve) => server!.once("listening", resolve));
    return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  }

  it("answers what Stilebase says, serves people, and forwards the request line and headers", async () => {
    const base = await site();
    expect((await fetch(`${base}/listings`, { headers: GPTBOT })).status).toBe(402);
    const person = await fetch(`${base}/listings?x=1`, { headers: { ...BROWSER, cookie: "s=1; stilebase_pass=abc" } });
    expect([person.status, await person.text()]).toEqual([200, "the listings"]);
    const body = stilebase.to(DECIDE)[1]!.body!;
    expect(body).toMatchObject({ request: { method: "GET", scheme: "https", path: "/listings?x=1" }, cookies: { stilebase_pass: "abc" }, credentials: true });
    expect((body.headers as Array<[string, string]>).some(([n]) => n === "cookie")).toBe(false);
  });

  it("holds a paid answer and sends it with the receipt once settled", async () => {
    stilebase.decide = holding();
    const base = await site();
    const paid = await fetch(`${base}/stream`, { headers: PAID });
    expect([paid.status, await paid.text(), paid.headers.get("payment-response")]).toEqual([200, "part one, part two", "eyJzdWNjZXNzIjp0cnVlfQ=="]);
    expect(stilebase.to(SETTLE)[0]!.body).toMatchObject({ delivery: { status: 200, size: 18 } });
  });

  it("never sends a held answer the settlement refused, or one too large to hold", async () => {
    stilebase.decide = holding();
    stilebase.settle = { status: 200, body: { action: "respond", status: 402, headers: { "content-type": "text/plain" }, body: "refused" } };
    const base = await site();
    const refused = await fetch(`${base}/listings`, { headers: PAID });
    expect([refused.status, await refused.text(), refused.headers.get("x-site")]).toEqual([402, "refused", null]);
    const big = await fetch(`${base}/big`, { headers: PAID });
    expect(big.status).toBe(502);
    expect(stilebase.to(SETTLE)).toHaveLength(1); // the big one wasn't settled
  });

  it("stops reading a paid answer as soon as it's too large", async () => {
    stilebase.decide = holding(10);
    const base = await site();
    const paid = await fetch(`${base}/stream`, { headers: PAID });
    expect(paid.status).toBe(502);
    expect(stilebase.to(SETTLE)).toEqual([]);
  });

  it("keeps the route's end callback while holding", async () => {
    stilebase.decide = holding();
    const paid = await fetch(`${await site()}/callback`, { headers: PAID });
    expect([paid.status, await paid.text()]).toEqual([200, "done"]);
  });

  it("leaves out a header Stilebase sent that isn't valid, and sends the rest", async () => {
    stilebase.decide = () => ({ status: 200, body: { action: "respond", status: 402, headers: { "x-bad": "a\nb", "x-ok": "1" }, body: "pay" } });
    const refused = await fetch(`${await site()}/listings`, { headers: PAID });
    expect([refused.status, await refused.text(), refused.headers.get("x-ok"), refused.headers.get("x-bad")]).toEqual([402, "pay", "1", null]);
  });

  it("sends Stilebase's answer with only headers Node accepts (a real server)", async () => {
    const headers = { "x-ok": "a b", "x-euro": "pay €1", "x-ctl": "a\u0001b", "x-space": " lead", connection: "close", "content-length": "99" };
    stilebase.decide = () => ({ status: 200, body: { action: "respond", status: 402, headers, body: "pay" } });
    const answered = await fetch(`${await site()}/listings`, { headers: GPTBOT });
    expect([answered.status, await answered.text(), answered.headers.get("x-ok"), answered.headers.get("x-euro"), answered.headers.get("x-ctl")]).toEqual([402, "pay", "a b", null, null]);
    expect(answered.headers.get("content-length")).toBe("3");
  });

  it("lets Stilebase's headers win over the route's, whenever it sets them", async () => {
    stilebase.decide = () => ({ status: 200, body: { action: "pass", response_headers: { "x-site": "stilebase" } } });
    const served = await fetch(`${await site()}/listings`, { headers: BROWSER });
    expect([await served.text(), served.headers.get("x-site")]).toEqual(["the listings", "stilebase"]);
  });

  it("reports what the site answered a paid request, so a failure isn't charged", async () => {
    stilebase.decide = holding();
    stilebase.settle = { status: 200, body: { action: "pass", response_headers: { "payment-response": "bm90" } } };
    const base = await site();
    const broken = await fetch(`${base}/broken`, { headers: PAID });
    expect([broken.status, await broken.text()]).toEqual([500, "oops"]);
    expect(stilebase.to(SETTLE)[0]!.body).toMatchObject({ delivery: { status: 500, size: 4 } });
  });

  it("copies an answer for capture without changing it", async () => {
    stilebase.decide = () => ({ status: 200, body: { action: "pass", capture: { token: "t", max_bytes: 8 } } });
    const base = await site();
    const served = await fetch(`${base}/stream`, { headers: BROWSER });
    expect(await served.text()).toBe("part one, part two");
    await new Promise((r) => setTimeout(r, 20));
    expect(stilebase.to(CAPTURES)[0]!.body).toMatchObject({ token: "t", status: 200, body: btoa("part one"), size: 18, truncated: true });
  });

  it("takes the visitor's address from req.ip, or the header you name (its last entry)", async () => {
    await fetch(`${await site()}/listings`, { headers: { ...GPTBOT, "x-forwarded-for": "20.15.240.70" } });
    expect(stilebase.to(DECIDE)[0]!.body!.client_ip).toBe("127.0.0.1");
    server?.close();
    stilebase = new FakeStilebase();
    await fetch(`${await site({ ipHeader: "x-forwarded-for" })}/listings`, { headers: { ...GPTBOT, "x-forwarded-for": "20.15.240.70, 198.51.100.9" } });
    expect(stilebase.to(DECIDE)[0]!.body!.client_ip).toBe("198.51.100.9");
  });
});

describe("fetch", () => {
  const handler = async (request: Request, ..._rest: unknown[]) => {
    const path = new URL(request.url).pathname;
    if (path === "/big") return new Response(BIG);
    if (path === "/broken") return new Response("oops", { status: 500 });
    return new Response("the listings", { headers: { "x-site": "yes" } });
  };
  const site = (options = {}) => withGate(handler, { key: KEY, apiUrl: API, fetch: stilebase.fetch, onError: () => undefined, ...options });

  it("answers what Stilebase says and serves people", async () => {
    const wrapped = site();
    expect((await wrapped(new Request("https://shop.example.com/listings", { headers: GPTBOT }))).status).toBe(402);
    const person = await wrapped(new Request("https://shop.example.com/listings", { headers: BROWSER }));
    expect(await person.text()).toBe("the listings");
  });

  it("holds and settles a paid answer; refuses too large ones", async () => {
    stilebase.decide = holding();
    const wrapped = site();
    const paid = await wrapped(new Request("https://shop.example.com/listings", { headers: PAID }));
    expect([paid.status, await paid.text(), paid.headers.get("payment-response"), paid.headers.get("x-site")]).toEqual([200, "the listings", "eyJzdWNjZXNzIjp0cnVlfQ==", "yes"]);
    expect((await wrapped(new Request("https://shop.example.com/big", { headers: PAID }))).status).toBe(502);
    stilebase.settle = { status: 200, body: { action: "unknown_payment", retry_after: 5 } };
    expect((await wrapped(new Request("https://shop.example.com/listings", { headers: PAID }))).status).toBe(503);
  });

  it("copies an answer for capture while the visitor gets it whole", async () => {
    stilebase.decide = () => ({ status: 200, body: { action: "pass", capture: { token: "t", max_bytes: 3 } } });
    const waits: Promise<unknown>[] = [];
    const wrapped = site();
    const served = await wrapped(new Request("https://shop.example.com/listings", { headers: BROWSER }), {}, { waitUntil: (p: Promise<unknown>) => waits.push(p) });
    expect(await served.text()).toBe("the listings");
    await Promise.all(waits);
    await new Promise((r) => setTimeout(r, 10));
    expect(stilebase.to(CAPTURES)[0]!.body).toMatchObject({ body: btoa("the"), size: 12, truncated: true });
  });

  it("never throws into the app over an answer the runtime refuses", async () => {
    stilebase.decide = () => ({ status: 200, body: { action: "respond", status: 204, headers: { "x-bad": "a\nb" }, body: "" } });
    const answered = await site()(new Request("https://shop.example.com/listings", { headers: GPTBOT }));
    expect([answered.status, answered.headers.get("x-bad")]).toEqual([204, null]);
    stilebase.decide = () => ({ status: 200, body: { action: "respond", status: 101, headers: {}, body: "" } });
    expect((await site()(new Request("https://shop.example.com/listings", { headers: GPTBOT }))).status).toBe(200); // unusable: passes
  });

  it("settles a paid 204 and sends it without a body, with decide's headers too", async () => {
    stilebase.decide = () => ({ status: 200, body: { action: "pass", settle_after: { max_bytes: 100 }, response_headers: { "x-from-decide": "1" } } });
    const empty = withGate(async () => new Response(null, { status: 204 }), { key: KEY, apiUrl: API, fetch: stilebase.fetch, onError: () => undefined });
    const paid = await empty(new Request("https://shop.example.com/x", { headers: PAID }));
    expect([paid.status, paid.headers.get("x-from-decide"), paid.headers.get("payment-response")]).toEqual([204, "1", "eyJzdWNjZXNzIjp0cnVlfQ=="]);
  });

  it("doesn't hold or charge a protocol switch", async () => {
    stilebase.decide = holding();
    const upgrade = withGate(async () => {
      const response = new Response(null, { status: 200 });
      Object.defineProperty(response, "status", { value: 101 });
      return response;
    }, { key: KEY, apiUrl: API, fetch: stilebase.fetch, onError: () => undefined });
    const answered = await upgrade(new Request("https://shop.example.com/ws", { headers: PAID }));
    expect([answered.status, await answered.text()]).toEqual([502, '{"error":"answer_not_payable"}']);
    expect(stilebase.to(SETTLE)).toEqual([]);
  });

  it("never sends the site's cookies in a capture", async () => {
    stilebase.decide = () => ({ status: 200, body: { action: "pass", capture: { token: "t", max_bytes: 10 } } });
    const cookies = withGate(async () => new Response("hi", { headers: { "set-cookie": "session=secret", "x-a": "1" } }), { key: KEY, apiUrl: API, fetch: stilebase.fetch, onError: () => undefined });
    const served = await cookies(new Request("https://shop.example.com/", { headers: BROWSER }));
    expect([served.headers.get("set-cookie"), await served.text()]).toEqual(["session=secret", "hi"]);
    await new Promise((r) => setTimeout(r, 10));
    const sent = stilebase.to(CAPTURES)[0]!.body!.headers as Array<[string, string]>;
    expect(sent.map(([n]) => n)).not.toContain("set-cookie");
  });

  it("reads the Worker's env and Cloudflare's edge facts", async () => {
    const wrapped = withGate(handler, { apiUrl: API, fetch: stilebase.fetch, onError: () => undefined });
    const request = new Request("https://shop.example.com/listings", { headers: { ...GPTBOT, "cf-connecting-ip": "198.51.100.9" } });
    Object.defineProperty(request, "cf", { value: { asn: 16509, httpProtocol: "HTTP/2" } });
    await wrapped(request, { STILEBASE_KEY: KEY }, {});
    expect(stilebase.to(DECIDE)[0]!.body).toMatchObject({ client_ip: "198.51.100.9", edge: { asn: 16509, http_protocol: "HTTP/2" } });
  });
});
