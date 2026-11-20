// The gate's own behaviour (SPEC.md sections 1, 3, 6, 7, 9), against a fake Stilebase.

import { beforeEach, describe, expect, it } from "vitest";

import { createGate } from "../src/core.js";
import { sha256Hex } from "../src/signing.js";
import { API, BROWSER, CAPTURES, DECIDE, FakeStilebase, GPTBOT, KEY, NOW, PAID, SETTINGS, SETTLE, request, settings } from "./fake-stilebase.js";

let stilebase: FakeStilebase;
let now: number;
const gate = (options: Parameters<typeof createGate>[0] = {}) =>
  createGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, now: () => now, onError: () => undefined, ...options });

beforeEach(() => {
  stilebase = new FakeStilebase();
  now = NOW;
});

describe("settings", () => {
  it("fetches them once, signed, and says what it is", async () => {
    const g = gate();
    await g.check(request("/a", BROWSER));
    await g.check(request("/b", BROWSER));
    const [call, ...more] = stilebase.to(SETTINGS);
    expect(more).toEqual([]);
    expect(call!.headers.get("stilebase-signature")).toMatch(/^v2=[0-9a-f]{64}$/);
    expect(call!.headers.get("stilebase-client")).toMatch(/^@stilebase\/sdk\/.+ gate\/core$/);
  });

  it("passes everyone without settings, when disabled, without a key, or when Stilebase switched it off", async () => {
    stilebase.settings = { status: 404 };
    expect((await gate().check(request("/", GPTBOT))).action).toBe("pass");
    stilebase = new FakeStilebase();
    expect((await gate({ disabled: true }).check(request("/", GPTBOT))).action).toBe("pass");
    expect((await gate({ key: "nope" }).check(request("/", GPTBOT))).action).toBe("pass");
    stilebase.settings = { status: 200, body: settings({ enabled: false }) };
    expect((await gate().check(request("/", GPTBOT))).action).toBe("pass");
    expect(stilebase.to(DECIDE)).toEqual([]);
  });

  it("passes everyone on settings this version can't read", async () => {
    const g = gate();
    await g.check(request("/", GPTBOT));
    stilebase.settings = { status: 200, body: { ...settings(), settings_version: 2 } };
    now += 301_000;
    await g.check(request("/", GPTBOT));
    await new Promise((r) => setTimeout(r, 0));
    expect((await g.check(request("/x", GPTBOT))).action).toBe("pass");
  });

  it("keeps the last good ones when a refresh isn't a JSON object", async () => {
    const g = gate();
    await g.check(request("/", GPTBOT));
    stilebase.settings = { status: 200, body: "null" };
    now += 301_000;
    await g.check(request("/", GPTBOT));
    await new Promise((r) => setTimeout(r, 0));
    expect((await g.check(request("/x", GPTBOT))).action).toBe("respond");
  });

  it("refreshes them, keeping the last good ones on failure", async () => {
    const g = gate();
    await g.check(request("/", GPTBOT));
    stilebase.settings = { status: 500 };
    now += 301_000;
    await g.check(request("/", GPTBOT));
    await new Promise((r) => setTimeout(r, 0));
    expect((await g.check(request("/x", GPTBOT))).action).toBe("respond"); // still asking: the last good settings
  });
});

describe("deciding", () => {
  it("asks Stilebase and does what it says", async () => {
    const g = gate();
    const agent = await g.check(request("/listings", GPTBOT));
    expect(agent).toMatchObject({ action: "respond", status: 402, headers: { "cache-control": "no-store" } });
    const [call] = stilebase.to(DECIDE);
    expect(call!.body).toMatchObject({ request: { method: "GET", scheme: "https", path: "/listings" }, client_ip: "203.0.113.7" });
    expect(call!.headers.get("stilebase-signature")).toMatch(/^v2=/);
  });

  it("skips static files and remembers people", async () => {
    const g = gate();
    expect((await g.check(request("/site.css", GPTBOT))).action).toBe("pass");
    await g.check(request("/listings", BROWSER));
    await g.check(request("/listings", BROWSER));
    expect(stilebase.to(DECIDE)).toHaveLength(1);
    now += 601_000; // remembered for 600 seconds
    await g.check(request("/listings", BROWSER));
    expect(stilebase.to(DECIDE)).toHaveLength(2);
  });

  it("never remembers a request with a payment, never skips one", async () => {
    stilebase.decide = () => ({ status: 200, body: { action: "pass", remember: { seconds: 600 } } });
    const g = gate();
    await g.check(request("/a.css", PAID));
    await g.check(request("/a.css", PAID));
    expect(stilebase.to(DECIDE)).toHaveLength(2);
  });

  it("asks once for requests arriving together", async () => {
    stilebase.decide = () => ({ status: 200, body: { action: "pass" }, delayMs: 50 });
    const g = gate();
    await Promise.all([1, 2, 3].map(() => g.check(request("/listings", BROWSER))));
    expect(stilebase.to(DECIDE)).toHaveLength(1);
  });

  it("lets the request through when Stilebase is slow or down; a payment gets 503", async () => {
    stilebase.settings = { status: 200, body: settings({ timeout_ms: 50 }) };
    stilebase.decide = () => ({ status: 200, body: { action: "respond", status: 402, headers: {}, body: "" }, delayMs: 1000 });
    const g = gate();
    expect((await g.check(request("/listings", GPTBOT))).action).toBe("pass");
    stilebase.decide = () => new Error("connection refused");
    expect((await g.check(request("/other", GPTBOT))).action).toBe("pass");
    expect(await g.check(request("/other", PAID))).toMatchObject({ action: "respond", status: 503, headers: { "retry-after": "5" } });
  });

  it("doesn't abort a decision past its budget: the connection it opens is kept for the next call", async () => {
    stilebase.settings = { status: 200, body: settings({ timeout_ms: 50 }) };
    stilebase.decide = () => ({ status: 200, body: { action: "pass" }, delayMs: 800 });
    const signals: AbortSignal[] = [];
    const watched = (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith(DECIDE) && init?.signal) signals.push(init.signal);
      return stilebase.fetch(input, init);
    };
    const kept: Promise<unknown>[] = [];
    const errors: string[] = [];
    const g = gate({ fetch: watched, onError: (e) => errors.push(e.message) });
    const started = Date.now();
    const decision = await g.check(request("/listings", GPTBOT), (p) => kept.push(p));
    expect(decision.action).toBe("pass"); // past its budget: the request goes on
    expect(Date.now() - started).toBeLessThan(600); // the budget, never the call (800 ms)
    expect(errors).toContain("Stilebase didn't answer within 50 ms");
    const pending = kept.length; // the settings load, and the decision past its budget: a Worker keeps them running
    await Promise.all(kept);
    expect(pending).toBe(2);
    expect(signals).toHaveLength(1);
    expect(signals[0]!.aborted).toBe(false); // it finished: its connection is open for the next call
  });

  it("never sends credentials, whatever the settings say", async () => {
    stilebase.settings = { status: 200, body: settings({ strip_headers: [] }) };
    await gate().check(request("/listings", { ...BROWSER, cookie: "s=1", authorization: "Bearer t", "proxy-authorization": "x" }));
    const names = (stilebase.to(DECIDE)[0]!.body!.headers as Array<[string, string]>).map(([n]) => n);
    expect(names).not.toContain("cookie");
    expect(names).not.toContain("authorization");
    expect(names).not.toContain("proxy-authorization");
  });

  it("remembers only a plain pass", async () => {
    stilebase.decide = () => ({ status: 200, body: { action: "pass", remember: { seconds: 600 }, response_headers: { "x-a": "1" } } });
    const g = gate();
    await g.check(request("/listings", BROWSER));
    await g.check(request("/listings", BROWSER));
    expect(stilebase.to(DECIDE)).toHaveLength(2);
  });

  it("shares a decision in flight only between GETs alike, and never its capture", async () => {
    stilebase.decide = () => ({ status: 200, body: { action: "pass", capture: { token: "t", max_bytes: 10 } }, delayMs: 50 });
    const g = gate();
    const [first, second] = await Promise.all([g.check(request("/listings", BROWSER)), g.check(request("/listings", BROWSER))]);
    expect(stilebase.to(DECIDE)).toHaveLength(1);
    expect([first, second].filter((d) => d.action === "pass" && d.capture)).toHaveLength(1);
    await Promise.all([g.check(request("/x", BROWSER)), g.check(request("/x", BROWSER, { method: "POST" }))]);
    expect(stilebase.to(DECIDE)).toHaveLength(3);
  });

  it("asks once when half open, and forgets a shared decision past its budget", async () => {
    stilebase.decide = () => ({ status: 500 });
    const g = gate();
    for (let i = 0; i < 5; i += 1) await g.check(request(`/p${i}`, GPTBOT));
    now += 31_000;
    stilebase.decide = () => ({ status: 200, body: { action: "pass" }, delayMs: 30 });
    await Promise.all([g.check(request("/a", GPTBOT)), g.check(request("/b", GPTBOT)), g.check(request("/c", GPTBOT))]);
    expect(stilebase.to(DECIDE)).toHaveLength(6); // one probe
    await g.check(request("/d", GPTBOT));
    expect(stilebase.to(DECIDE)).toHaveLength(7); // closed
  });

  it("forgets a half-open probe that never finished", async () => {
    stilebase.decide = () => ({ status: 500 });
    const g = gate();
    for (let i = 0; i < 5; i += 1) await g.check(request(`/p${i}`, GPTBOT));
    now += 31_000;
    stilebase.decide = () => ({ status: 200, body: { action: "pass" }, delayMs: 100_000 }); // a probe that hangs
    void g.check(request("/probe", GPTBOT));
    await new Promise((r) => setTimeout(r, 5));
    expect(stilebase.to(DECIDE)).toHaveLength(6);
    now += 2_000; // past its budget and a second
    stilebase.decide = () => ({ status: 200, body: { action: "pass" } });
    await g.check(request("/next", GPTBOT));
    expect(stilebase.to(DECIDE)).toHaveLength(7);
  });

  it("lets only the request that asked remember", async () => {
    stilebase.decide = () => ({ status: 200, body: { action: "pass", remember: { seconds: 600 }, capture: { token: "t", max_bytes: 5 } }, delayMs: 30 });
    const g = gate();
    await Promise.all([g.check(request("/l", BROWSER)), g.check(request("/l", BROWSER))]);
    await g.check(request("/l", BROWSER));
    expect(stilebase.to(DECIDE)).toHaveLength(2); // nobody remembered a pass that carried a capture
  });

  it("settles even when the settings were dropped meanwhile", async () => {
    stilebase.decide = () => ({ status: 200, body: { action: "pass", settle_after: { max_bytes: 100 } } });
    const g = gate();
    const held = await g.check(request("/x", PAID));
    if (held.action !== "hold") throw new Error("not held");
    stilebase.settings = { status: 404 };
    now += 301_000;
    await g.check(request("/y", BROWSER));
    await new Promise((r) => setTimeout(r, 0));
    expect((await g.settle(held.ticket, { status: 200, body: new Uint8Array() })).action).toBe("send");
  });

  it("is switched off by STILEBASE_DISABLED=true too", async () => {
    process.env.STILEBASE_DISABLED = " True ";
    try {
      await gate({ disabled: undefined }).check(request("/", GPTBOT));
      expect(stilebase.calls).toEqual([]);
    } finally {
      delete process.env.STILEBASE_DISABLED;
    }
  });

  it("stops asking for a while after failures in a row (the breaker)", async () => {
    stilebase.decide = () => ({ status: 500 });
    const g = gate();
    for (let i = 0; i < 5; i += 1) await g.check(request(`/p${i}`, GPTBOT));
    expect(stilebase.to(DECIDE)).toHaveLength(5);
    await g.check(request("/p6", GPTBOT));
    expect(stilebase.to(DECIDE)).toHaveLength(5); // open: everyone passes, nothing asked
    expect(await g.check(request("/p7", PAID))).toMatchObject({ status: 503 });
    now += 31_000;
    await g.check(request("/p8", GPTBOT)); // one more failure reopens it
    await g.check(request("/p9", GPTBOT));
    expect(stilebase.to(DECIDE)).toHaveLength(6);
    now += 31_000;
    stilebase.decide = () => ({ status: 200, body: { action: "pass" } });
    await g.check(request("/p10", GPTBOT)); // one usable answer closes it
    await g.check(request("/p11", GPTBOT));
    expect(stilebase.to(DECIDE)).toHaveLength(8);
  });
});

describe("a payment", () => {
  beforeEach(() => {
    stilebase.decide = () => ({ status: 200, body: { action: "pass", settle_after: { max_bytes: 1000 } } });
  });

  it("is held, then settled with what the site answered", async () => {
    const g = gate();
    const decision = await g.check(request("/listings", PAID));
    expect(decision).toMatchObject({ action: "hold", maxBytes: 1000 });
    if (decision.action !== "hold") return;
    const body = new TextEncoder().encode("the listings");
    const settled = await g.settle(decision.ticket, { status: 200, body });
    expect(settled).toEqual({ action: "send", responseHeaders: { "payment-response": "eyJzdWNjZXNzIjp0cnVlfQ==" } });
    const [call] = stilebase.to(SETTLE);
    expect(call!.body).toMatchObject({ request: { path: "/listings" }, delivery: { status: 200, size: 12, sha256: await sha256Hex(body) } });
  });

  it("isn't sent when the settlement fails or can't be told", async () => {
    const g = gate();
    const decision = await g.check(request("/listings", PAID));
    if (decision.action !== "hold") throw new Error("not held");
    stilebase.settle = { status: 200, body: { action: "respond", status: 402, headers: {}, body: "refused" } };
    expect(await g.settle(decision.ticket, { status: 200, body: new Uint8Array() })).toMatchObject({ action: "respond", status: 402 });
    stilebase.settle = { status: 502 };
    expect(await g.settle(decision.ticket, { status: 200, body: new Uint8Array() })).toMatchObject({ action: "respond", status: 503 });
    stilebase.settle = { status: 200, body: { action: "unknown_payment", retry_after: 7 } };
    expect(await g.settle(decision.ticket, { status: 200, body: new Uint8Array() })).toMatchObject({ headers: { "retry-after": "7" } });
    expect(g.tooLarge()).toMatchObject({ status: 502 });
  });
});

describe("capture", () => {
  it("sends a copy in the background", async () => {
    stilebase.decide = () => ({ status: 200, body: { action: "pass", capture: { token: "t1", max_bytes: 4 } } });
    const g = gate();
    const decision = await g.check(request("/listings", BROWSER));
    expect(decision).toMatchObject({ action: "pass", capture: { token: "t1", maxBytes: 4 } });
    g.capture({ token: "t1", maxBytes: 4 }, { status: 200, headers: [["Content-Type", "text/html"]], body: new TextEncoder().encode("the "), size: 12, truncated: true, ttfbMs: 3.4 });
    await new Promise((r) => setTimeout(r, 10));
    const [call] = stilebase.to(CAPTURES);
    expect(call!.body).toEqual({ token: "t1", status: 200, headers: [["content-type", "text/html"]], body: btoa("the "), size: 12, truncated: true, ttfb_ms: 3 });
  });
});
