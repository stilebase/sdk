import { beforeEach, describe, expect, it, vi } from "vitest";

import { type GateOptions, createGate } from "../src/core.js";
import { passToken } from "../src/signing.js";
import { API, BROWSER, CHROME_UA, DOMAIN, FakeStilebase, GATEWAY_ID, GPTBOT, KEY, NOW, REQUIRED, liveConfig, request } from "./fake-stilebase.js";

let stilebase: FakeStilebase;
let clock: number;
let errors: Error[];

function gate(options: GateOptions = {}) {
  return createGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, now: () => clock, onError: (e) => errors.push(e), ...options }, "test");
}

/** Lets background reports finish. */
const settled = () => new Promise((resolve) => setTimeout(resolve, 25));

beforeEach(() => {
  stilebase = new FakeStilebase();
  clock = NOW;
  errors = [];
});

describe("people and everything that isn't an agent", () => {
  it("pass without asking Stilebase about them (the settings and address lists load once)", async () => {
    const g = gate();
    expect(await g.check(request("/listings", BROWSER))).toEqual({ action: "pass" });
    expect(await g.check(request("/listings", BROWSER))).toEqual({ action: "pass" });
    expect(stilebase.calls.map((c) => c.path)).toEqual([`/v1/x402/sites/${GATEWAY_ID}/config`, "/v1/x402/agent-ranges"]);
  });

  it("pass when the gate has no key, and it says so", async () => {
    const keyless = createGate({ key: "", fetch: stilebase.fetch, onError: (e) => errors.push(e) });
    expect(await keyless.check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
    expect(errors[0]?.message).toMatch(/STILEBASE_KEY isn't set/);
    expect(createGate({ key: "sbsec_old", onError: (e) => errors.push(e) })).toBeTruthy();
    expect(errors[1]?.message).toMatch(/isn't a site key/);
    expect(stilebase.calls).toHaveLength(0);
  });
});

describe("settings", () => {
  it("are fetched signed, saying which package this is", async () => {
    await gate().check(request("/", BROWSER));
    const [call] = stilebase.calls;
    expect(call?.headers.get("stilebase-client")).toMatch(/^@stilebase\/sdk\/\d+\.\d+\.\d+ gate\/test$/);
    expect(call?.headers.get("stilebase-timestamp")).toBe(String(NOW / 1000));
    expect(call?.headers.get("stilebase-signature")).toMatch(/^v2=[0-9a-f]{64}$/);
  });

  it("a refused key lets everyone through", async () => {
    stilebase.config = { status: 401 };
    expect(await gate().check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
    expect(errors[0]?.message).toMatch(/refused the site key/);
  });

  it("when Stilebase is down at start, requests don't each wait for it", async () => {
    stilebase.config = new Error("connect ECONNREFUSED");
    const g = gate();
    expect(await g.check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
    clock += 10_000;
    expect(await g.check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
    expect(stilebase.to(`/v1/x402/sites/${GATEWAY_ID}/config`)).toHaveLength(1);
    stilebase.config = { status: 200, body: liveConfig() };
    clock += 30_000; // tried again in the background
    await g.check(request("/listings", GPTBOT));
    await settled();
    expect((await g.check(request("/listings", GPTBOT))).action).toBe("respond");
  });

  it("are refreshed in the background, keeping the last good ones on failure", async () => {
    const g = gate();
    await g.check(request("/", BROWSER));
    clock += 299_000;
    await g.check(request("/", BROWSER));
    expect(stilebase.to(`/v1/x402/sites/${GATEWAY_ID}/config`)).toHaveLength(1);
    stilebase.config = { status: 500 };
    clock += 2_000;
    await g.check(request("/", BROWSER));
    await settled();
    expect(stilebase.to(`/v1/x402/sites/${GATEWAY_ID}/config`)).toHaveLength(2);
    expect((await g.check(request("/listings", GPTBOT))).action).toBe("respond"); // still gated
  });

  it("serve the verification file until the domain is proven, even before it's live", async () => {
    stilebase.config = {
      status: 200,
      body: liveConfig({ gateway: { id: GATEWAY_ID, domain: DOMAIN, live: false, reason: "domain_not_verified" }, verification: { path: "/.well-known/stilebase-verify.txt", value: "stilebase-verify=tok" } }),
    };
    const g = gate();
    expect(await g.check(request("/.well-known/stilebase-verify.txt"))).toEqual({
      action: "respond",
      status: 200,
      headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
      body: "stilebase-verify=tok\n",
    });
    expect(await g.check(request("/listings", GPTBOT))).toEqual({ action: "pass" }); // not live: nobody is asked
    expect(stilebase.to("/v1/x402/requirements")).toHaveLength(0);
  });
});

describe("AI agents", () => {
  it("get the 402 with Stilebase's requirement, and the visit is reported", async () => {
    const decision = await gate().check(request("/listings?city=Haifa", GPTBOT, { ip: "198.51.100.4" }));
    expect(decision.action).toBe("respond");
    if (decision.action !== "respond") return;
    expect(decision.status).toBe(402);
    expect(JSON.parse(atob(decision.headers["PAYMENT-REQUIRED"]!))).toEqual(REQUIRED);
    expect(JSON.parse(decision.body)).toEqual(REQUIRED);
    const [asked] = stilebase.to("/v1/x402/requirements");
    expect(Object.fromEntries(asked!.query)).toEqual({ host: DOMAIN, url: `https://${DOMAIN}/listings?city=Haifa`, ua: GPTBOT["user-agent"], ip: "198.51.100.4" });
    await settled();
    const [visit] = stilebase.to("/v1/x402/visits");
    expect(visit?.body).toEqual({ host: DOMAIN, request: { method: "GET", path: "/listings?city=Haifa" }, agent: { ip: "198.51.100.4", user_agent: GPTBOT["user-agent"], headers: {} } });
    expect(visit?.headers.get("stilebase-signature")).toMatch(/^v2=/);
  });

  it("with a free pass read for free, reported as one", async () => {
    stilebase.requirements = () => ({ status: 204, headers: { "cache-control": "public, max-age=300" } });
    expect(await gate().check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
    await settled();
    expect(stilebase.to("/v1/x402/visits")[0]?.body).toMatchObject({ free_pass: true });
  });

  it("are asked once per requirement for five minutes", async () => {
    const g = gate();
    await g.check(request("/listings", GPTBOT));
    await g.check(request("/listings", GPTBOT));
    expect(stilebase.to("/v1/x402/requirements")).toHaveLength(1);
    clock += 301_000;
    await g.check(request("/listings", GPTBOT));
    expect(stilebase.to("/v1/x402/requirements")).toHaveLength(2);
  });

  it("pass when Stilebase has no live gateway or can't be reached", async () => {
    stilebase.requirements = () => ({ status: 404 });
    expect(await gate().check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
    stilebase.requirements = () => new Error("timeout");
    expect(await gate().check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
  });

  it("starts when the runtime refuses to read one environment variable (Deno's --allow-env for the key only)", async () => {
    const original = process.env;
    process.env = new Proxy(original, {
      get(target, name) {
        if (name === "STILEBASE_API_URL") throw new Error("NotCapable: requires env access to STILEBASE_API_URL");
        return Reflect.get(target, name);
      },
    });
    try {
      const g = createGate({ key: KEY, fetch: (input, init) => stilebase.fetch(String(input).replace("https://stilebase.com", API), init), onError: (e) => errors.push(e) }, "test");
      expect((await g.check(request("/listings/2", GPTBOT))).action).toBe("respond"); // the default API, gating as usual
    } finally {
      process.env = original;
    }
  });

  it("reports free entries that can never match, once", async () => {
    const g = gate({ free: ["healthz", "/robots.txt?x", "/a/../b", "/a#b", "/%68ealthz", "/healthz"] });
    expect(await g.check(request("/healthz", GPTBOT))).toEqual({ action: "pass" });
    await g.check(request("/healthz", GPTBOT));
    const reports = errors.filter((e) => /can never match/.test(e.message));
    expect(reports).toHaveLength(1);
    for (const entry of ['"healthz"', '"/robots.txt?x"', '"/a/../b"', '"/a#b"', '"/%68ealthz"']) expect(reports[0]?.message).toContain(entry);
    expect(reports[0]?.message.split(" can never match")[0]).not.toContain('"/healthz"'); // the usable entry isn't listed
    errors.length = 0;
    await gate({ free: null as unknown as string[] }).check(request("/healthz", GPTBOT)); // null: no free paths, no report
    expect(errors.filter((e) => /can never match/.test(e.message))).toHaveLength(0);
    // An entry JSON can't write (a BigInt) is reported by its type, and never makes the gate fail open.
    const odd = gate({ free: ["/healthz", 1n as unknown as string] });
    for (let i = 0; i < 2; i++) expect((await odd.check(request("/premium/report", GPTBOT))).action).toBe("respond");
    expect(errors.filter((e) => /bigint can never match/.test(e.message))).toHaveLength(1);
  });

  it("everywhere except the free paths you list, exactly", async () => {
    const g = gate({ free: ["/healthz", "/robots.txt"] });
    expect(await g.check(request("/healthz", GPTBOT))).toEqual({ action: "pass" });
    expect(await g.check(request("/robots.txt?x=1", GPTBOT))).toEqual({ action: "pass" });
    for (const path of ["/listings/2", "/healthz/", "/HEALTHZ", "/healthz/../listings/2", "/x/../healthz", "//healthz", "/%68ealthz"]) {
      expect((await g.check(request(path, GPTBOT))).action, path).toBe("respond");
    }
  });

  it("in test mode, only Stilebase's test agent is asked", async () => {
    const g = gate({ mode: "test" });
    expect(await g.check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
    expect(await g.check(request("/listings", { "user-agent": "curl/8.4.0" }))).toEqual({ action: "pass" });
    expect((await g.check(request("/listings", { "user-agent": "Mozilla/5.0 (compatible; StilebaseCheck/1.0)" }))).action).toBe("respond");
  });
});

describe("hidden agents and scripts", () => {
  it("scripts by their default user agent pay, unless let through", async () => {
    expect((await gate().check(request("/listings", { "user-agent": "curl/8.4.0" }))).action).toBe("respond");
    expect((await gate().check(request("/listings", {}))).action).toBe("respond"); // no user agent at all
    expect(await gate({ catchHttpClients: false }).check(request("/listings", { "user-agent": "curl/8.4.0" }))).toEqual({ action: "pass" });
  });

  it("agents from their operators' addresses or signing with Web Bot Auth pay, behind a browser's user agent", async () => {
    expect((await gate().check(request("/listings", BROWSER, { ip: "20.15.240.70" }))).action).toBe("respond");
    expect((await gate().check(request("/listings", { ...BROWSER, "signature-agent": '"https://chatgpt.com"' }))).action).toBe("respond");
    expect(await gate().check(request("/listings", BROWSER, { ip: "20.15.240.80" }))).toEqual({ action: "pass" });
    expect(await gate({ catchHiddenAgents: false }).check(request("/listings", BROWSER, { ip: "20.15.240.70" }))).toEqual({ action: "pass" });
    expect(stilebase.to("/v1/x402/agent-ranges").length).toBeGreaterThan(0);
  });
});

describe("browser-looking requests that don't act like a browser", () => {
  const SCRIPT = { "user-agent": CHROME_UA, accept: "*/*" };

  it("are only counted while strict detection is off", async () => {
    stilebase.requirements = (q) => (q.get("suspect") === "true" ? { status: 204 } : { status: 200, body: REQUIRED });
    expect(await gate().check(request("/listings", SCRIPT))).toEqual({ action: "pass" });
    await settled();
    expect(stilebase.to("/v1/x402/visits")[0]?.body).toMatchObject({ signals: ["no_accept_language", "no_sec_fetch", "chrome_without_client_hints", "accept_any"] });
  });

  it("get a page that lets real browsers through under strict detection", async () => {
    const decision = await gate().check(request("/listings", { ...SCRIPT, accept: "text/html" }));
    expect(decision.action === "respond" && decision.status).toBe(402);
    if (decision.action !== "respond") return;
    expect(decision.headers["content-type"]).toMatch(/text\/html/);
    expect(decision.body).toContain("stilebase_pass=");
    expect(decision.headers["PAYMENT-REQUIRED"]).toBeTruthy();
  });

  it("with the browser check's cookie pass without asking Stilebase", async () => {
    const token = await passToken(`sbsec_${"ab".repeat(32)}`, CHROME_UA, NOW / 1000 - 60);
    const g = gate();
    expect(await g.check(request("/listings", { ...SCRIPT, cookie: `a=1; stilebase_pass=${token}` }))).toEqual({ action: "pass" });
    expect(stilebase.to("/v1/x402/requirements")).toHaveLength(0);
    const forged = `${NOW / 1000}.${"0".repeat(32)}`;
    expect((await g.check(request("/listings", { ...SCRIPT, cookie: `stilebase_pass=${forged}` }))).action).toBe("respond");
  });

  it("are checked when the app sees plain http (a proxy ended TLS), unless the site really is http, or checks are off", async () => {
    const overHttp = request("/listings", SCRIPT, { url: `http://${DOMAIN}/listings` });
    expect((await gate().check(overHttp)).action).toBe("respond");
    expect(await gate({ assumeHttps: false }).check(overHttp)).toEqual({ action: "pass" });
    expect(await gate({ checkBrowsers: false }).check(request("/listings", SCRIPT))).toEqual({ action: "pass" });
  });
});

describe("payments", () => {
  const PAID = { ...GPTBOT, "payment-signature": "eyJ4NDAyVmVyc2lvbiI6Mn0=" };

  it("settled through Stilebase let the request through, with PAYMENT-RESPONSE", async () => {
    expect(await gate().check(request("/listings/2", PAID, { ip: "198.51.100.4" }))).toEqual({ action: "pass", paymentResponse: "eyJzdWNjZXNzIjp0cnVlfQ==" });
    const [settle] = stilebase.to("/v1/x402/settle");
    expect(settle?.body).toEqual({
      host: DOMAIN,
      payment: PAID["payment-signature"],
      request: { method: "GET", path: "/listings/2" },
      agent: { ip: "198.51.100.4", user_agent: GPTBOT["user-agent"], headers: {} },
    });
  });

  it("x402 v1's X-PAYMENT header too", async () => {
    await gate().check(request("/listings/2", { ...GPTBOT, "x-payment": "v1payment" }));
    expect(stilebase.to("/v1/x402/settle")[0]?.body).toMatchObject({ payment: "v1payment" });
  });

  it("a receipt that can't be a header is dropped, never a broken answer", async () => {
    stilebase.settle = { status: 200, body: { success: true, transaction: "0xabc", network: "eip155:8453", header: "bad\r\nvalue" } };
    expect(await gate().check(request("/listings/2", PAID))).toEqual({ action: "pass" });
    stilebase.settle = { status: 200, body: { success: false, errorReason: "insufficient_funds", header: "bad\r\nvalue" } };
    const refused = await gate().check(request("/listings/2", PAID));
    expect(refused.action === "respond" && refused.status).toBe(402);
    if (refused.action === "respond") expect(refused.headers["PAYMENT-RESPONSE"]).toBeUndefined();
  });

  it("an address that isn't a string (a custom clientIp's mistake) is none, and agents still pay", async () => {
    const decision = await gate().check({ ...request("/listings/2", GPTBOT), ip: { hostname: "1.2.3.4" } as unknown as string });
    expect(decision.action === "respond" && decision.status).toBe(402);
  });

  it("an empty PAYMENT-SIGNATURE doesn't hide an X-PAYMENT", async () => {
    await gate().check(request("/listings/2", { ...GPTBOT, "payment-signature": "", "x-payment": "v1payment" }));
    expect(stilebase.to("/v1/x402/settle")[0]?.body).toMatchObject({ payment: "v1payment" });
  });

  it("refused get the 402 again, with the reason", async () => {
    stilebase.settle = { status: 200, body: { success: false, errorReason: "invalid_exact_evm_payload_signature", header: "eyJmYWlsIjp0cnVlfQ==" } };
    const decision = await gate().check(request("/listings/2", PAID));
    expect(decision.action === "respond" && decision.status).toBe(402);
    if (decision.action !== "respond") return;
    expect(JSON.parse(decision.body).error).toBe("invalid_exact_evm_payload_signature");
    expect(decision.headers["PAYMENT-RESPONSE"]).toBe("eyJmYWlsIjp0cnVlfQ==");
  });

  it("that Stilebase doesn't confirm get 503, and nothing is given away", async () => {
    for (const answer of [{ status: 502 }, new Error("timeout"), { status: 200, body: null }, { status: 200, body: [] }, { status: 200, body: 1 }, { status: 200, body: {} }, { status: 200, body: { success: "false" } }]) {
      stilebase.settle = answer;
      const decision = await gate().check(request("/listings/2", PAID));
      expect(decision).toMatchObject({ action: "respond", status: 503, headers: { "retry-after": "5" } });
    }
  });
});

describe("never breaking the site", () => {
  it("a failing error handler still lets the request through", async () => {
    stilebase.config = { status: 401 };
    const g = createGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, onError: () => { throw new Error("boom"); } });
    expect(await g.check(request("/", GPTBOT))).toEqual({ action: "pass" });
  });

  it("an agent can't dodge the gate with a URL the parser rejects (it's read on the gateway's domain)", async () => {
    for (const url of ["not a url", "https://999.999.999.999/listings/1", "https://example.com:99999/listings/1", "https://[::::]/x"]) {
      expect((await gate().check({ ...request("/", GPTBOT), url })).action, url).toBe("respond");
    }
  });

  it("warns on the console once in a while by default", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      stilebase.config = { status: 401 };
      const g = createGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, now: () => clock });
      await g.check(request("/", GPTBOT));
      clock += 31_000;
      await g.check(request("/", GPTBOT));
      await settled();
      expect(warn).toHaveBeenCalledTimes(1);
    } finally {
      warn.mockRestore();
    }
  });
});

describe("the review's cases (cycle 1)", () => {
  it("cuts an over-long URL or user agent to Stilebase's limits, so its call can't be refused and serve the page", async () => {
    const decision = await gate().check(request(`/a?pad=${"x".repeat(5000)}`, { "user-agent": `GPTBot/1.2 ${"y".repeat(3000)}` }));
    expect(decision.action).toBe("respond");
    const [asked] = stilebase.to("/v1/x402/requirements");
    expect([asked!.query.get("url")!.length, asked!.query.get("ua")!.length]).toEqual([2048, 512]);
    await settled();
    const visit = stilebase.to("/v1/x402/visits")[0]?.body as { request: { path: string }; agent: { user_agent: string } };
    expect([visit.request.path.length, visit.agent.user_agent.length]).toEqual([2048, 512]);
  });

  it("doesn't run on settings of a version it can't read", async () => {
    stilebase.config = { status: 200, body: liveConfig({ config_version: 2 }) };
    expect(await gate().check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
    expect(errors[0]?.message).toMatch(/can't read/);
  });
});

describe("the review's cases (cycle 2)", () => {
  it("asks scripts to pay from loopback too (a same-host proxy makes every visitor look local); owners exempt health checks", async () => {
    expect((await gate().check(request("/", { "user-agent": "curl/8.4.0" }, { ip: "127.0.0.1" }))).action).toBe("respond");
    expect(await gate({ free: ["/healthz"] }).check(request("/healthz", { "user-agent": "curl/8.4.0" }, { ip: "127.0.0.1" }))).toEqual({ action: "pass" });
    expect(await gate({ catchHttpClients: false }).check(request("/", { "user-agent": "curl/8.4.0" }, { ip: "127.0.0.1" }))).toEqual({ action: "pass" });
  });

  it("takes a Fetch Request as it is", async () => {
    const decision = await gate().check(new Request(`https://${DOMAIN}/listings`, { headers: GPTBOT }));
    expect(decision.action).toBe("respond");
  });

  it("refreshes settings that don't say how often (every 5 minutes)", async () => {
    const config = liveConfig() as unknown as Record<string, unknown>;
    delete config.refresh_seconds;
    stilebase.config = { status: 200, body: config };
    const g = gate();
    await g.check(request("/", BROWSER));
    clock += 301_000;
    await g.check(request("/", BROWSER));
    await settled();
    expect(stilebase.to(`/v1/x402/sites/${GATEWAY_ID}/config`)).toHaveLength(2);
  });

  it("treats an empty STILEBASE_API_URL as unset", async () => {
    const saved = process.env.STILEBASE_API_URL;
    process.env.STILEBASE_API_URL = "";
    const seen: string[] = [];
    try {
      const g = createGate({ key: KEY, fetch: (input, init) => (seen.push(String(input)), stilebase.fetch(input, init)), onError: () => undefined });
      await g.check(request("/", BROWSER));
    } finally {
      if (saved === undefined) delete process.env.STILEBASE_API_URL;
      else process.env.STILEBASE_API_URL = saved;
    }
    expect(seen[0]?.startsWith("https://stilebase.com/v1/x402/sites/")).toBe(true);
  });
});

describe("the review's cases (cycle 3)", () => {
  it("names the page by the gateway's domain over https, not the Host header (a proxy's app:3000)", async () => {
    await gate().check({ ...request("/listings/1?a=b", GPTBOT), url: "http://app:3000/listings/1?a=b" });
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("url")).toBe(`https://${DOMAIN}/listings/1?a=b`);
    await gate().check({ ...request("/", GPTBOT), url: "//evil.example/x" });
    expect(stilebase.to("/v1/x402/requirements")[1]?.query.get("url")).toBe(`https://${DOMAIN}/evil.example/x`);
  });

  it("keeps the price lookup's URL under proxies' 8 KB limit however the request is padded", async () => {
    await gate().check(request(`/listings/1?${"/".repeat(2048)}`, { "user-agent": `GPTBot/1.2 ${"é".repeat(512)}` }));
    const [asked] = stilebase.to("/v1/x402/requirements");
    expect(new URLSearchParams(asked!.query).toString().length).toBeLessThan(6000);
  });

  it("answers with the last price when Stilebase refuses a lookup, instead of serving the page", async () => {
    const g = gate();
    await g.check(request("/listings/1", GPTBOT));
    stilebase.requirements = () => ({ status: 414 });
    const decision = await g.check(request("/listings/2", GPTBOT));
    expect(decision.action === "respond" && decision.status).toBe(402);
    if (decision.action !== "respond") return;
    expect(JSON.parse(decision.body).resource.url).toBe(`https://${DOMAIN}/listings/2`);
    // With no price known yet, a refused lookup still never breaks the site.
    expect(await gate().check(request("/listings/2", GPTBOT))).toEqual({ action: "pass" });
  });

  it("answers a rate-limited lookup (429) with the last price too: many unique URLs mustn't read free", async () => {
    const g = gate();
    await g.check(request("/listings/1", GPTBOT));
    stilebase.requirements = () => ({ status: 429 });
    const decision = await g.check(request("/listings/2?x=1", GPTBOT));
    expect(decision.action === "respond" && decision.status).toBe(402);
  });

  it("reads a price only from a JSON object, and keeps the last one", async () => {
    const g = gate();
    await g.check(request("/listings/1", GPTBOT));
    stilebase.requirements = () => ({ status: 200, body: null });
    expect(await g.check(request("/listings/2", GPTBOT))).toEqual({ action: "pass" });
    stilebase.requirements = () => ({ status: 414 });
    const decision = await g.check(request("/listings/3", GPTBOT));
    expect(decision.action === "respond" && decision.status).toBe(402); // the last real price, not null
  });

  it("rejects an address range with an empty length instead of matching everyone", async () => {
    const { parseNetwork } = await import("../src/detect.js");
    expect(parseNetwork("20.15.240.64/")).toBeNull();
    expect(parseNetwork("20.15.240.64/33")).toBeNull();
  });
});

describe("the review's cases (cycle 4)", () => {
  it("measures the lookup as it's sent (form encoding), so parentheses can't blow the budget", async () => {
    await gate().check(request(`/a?${"(".repeat(2040)}`, { "user-agent": `GPTBot/1.2 ${"(".repeat(500)}` }));
    const [asked] = stilebase.to("/v1/x402/requirements");
    expect(new URLSearchParams(asked!.query).toString().length).toBeLessThan(6000);
  });

  it("calls a given fetch as a plain function (a fetch bound to the gate throws on Workers)", async () => {
    let self: unknown = "unset";
    const g = createGate({
      key: KEY,
      apiUrl: API,
      onError: () => undefined,
      fetch: function (this: unknown, input, init) {
        self = this; // eslint-disable-line @typescript-eslint/no-this-alias -- what the test checks
        return stilebase.fetch(input, init);
      },
    });
    await g.check(request("/", BROWSER));
    expect(self).toBeUndefined();
  });
});

describe("the review's cases (cycle 5)", () => {
  it("counts an over-long address as none, so it can't bloat the price lookup", async () => {
    await gate().check(request("/listings", GPTBOT, { ip: `198.51.100.4, ${"1.1.1.1, ".repeat(500)}` }));
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("ip")).toBe("");
  });
});

describe("the review's cases (cycle 7)", () => {
  it("keeps asking agents to pay when a refreshed key is refused (rotated, or the clock is off): only payments fail", async () => {
    const g = gate();
    await g.check(request("/listings", GPTBOT));
    stilebase.config = { status: 401 };
    clock += 301_000;
    await g.check(request("/", BROWSER)); // the refresh is refused
    await settled();
    expect((await g.check(request("/listings", GPTBOT))).action).toBe("respond"); // still the 402, never a free read
    expect(errors.some((e) => /refused the site key/.test(e.message))).toBe(true);
  });

  it("stops asking once the gateway is gone (404)", async () => {
    const g = gate();
    await g.check(request("/listings", GPTBOT));
    stilebase.config = { status: 404 };
    clock += 301_000;
    await g.check(request("/", BROWSER));
    await settled();
    expect(await g.check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
  });
});

describe("loads another request started", () => {
  it("never hold a request past its own deadline (a runtime may have cancelled the first one)", async () => {
    const g = createGate({ key: KEY, apiUrl: API, fetch: () => new Promise<Response>(() => undefined), onError: () => undefined });
    const started = Date.now();
    expect(await g.check(request("/listings/1", GPTBOT))).toEqual({ action: "pass" });
    expect(Date.now() - started).toBeLessThan(5_000);
  }, 10_000);

  it("keep the last address lists when an answer has none", async () => {
    const g = gate();
    await g.check(request("/listings/1", BROWSER, { ip: "20.15.240.70" }));
    clock += 2 * 60 * 60 * 1000;
    stilebase.ranges = { status: 200, body: {} };
    await g.check(request("/listings/1", BROWSER, { ip: "20.15.240.70" })); // refreshes in the background
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect((await g.check(request("/listings/1", BROWSER, { ip: "20.15.240.70" }))).action).toBe("respond");
  });
});

describe("the review's cases (cycle 13)", () => {
  it("frees nothing after a `#` (a forged Host on Deno puts one in the URL; a router reads past it)", async () => {
    const g = gate({ free: ["/healthz", "/"] });
    for (const url of [`https://${DOMAIN}/healthz#/listings/1`, `https://${DOMAIN}/#/listings/1`, `https://${DOMAIN}/healthz?a#/x`]) {
      expect((await g.check(request("/", GPTBOT, { url }))).action, url).toBe("respond");
    }
    expect((await g.check(request("/", GPTBOT, { url: `https://${DOMAIN}/healthz?a=1` }))).action).toBe("pass");
  });

  it("finds the browser check's cookie when a runtime joins Cookie fields with a comma", async () => {
    const token = await passToken(`sbsec_${"ab".repeat(32)}`, CHROME_UA, NOW / 1000 - 60);
    const script = { "user-agent": CHROME_UA, accept: "*/*" };
    expect(await gate().check(request("/listings", { ...script, cookie: `a=1, stilebase_pass=${token}` }))).toEqual({ action: "pass" });
  });

  it("answers a free path without waiting for the settings (a health check on a cold start)", async () => {
    let answer: (r: Response) => void = () => undefined;
    const slow = () => new Promise<Response>((resolve) => (answer = resolve));
    const g = createGate({ key: KEY, apiUrl: API, fetch: slow, onError: () => undefined, free: ["/healthz"] });
    const started = Date.now();
    expect(await g.check(request("/healthz", GPTBOT))).toEqual({ action: "pass" });
    expect(Date.now() - started).toBeLessThan(500);
    answer(new Response(null, { status: 500 }));
  });

  it("still serves the verification file at a free path once the settings are here", async () => {
    const path = "/.well-known/stilebase-verify.txt";
    stilebase.config = { status: 200, body: liveConfig({ verification: { path, value: "stilebase-verify=tok" } }) };
    const g = gate({ free: [path] });
    expect(await g.check(request(path))).toEqual({ action: "pass" }); // no settings yet: never waits for them
    await g.check(request("/listings", BROWSER));
    expect((await g.check(request(path))).action).toBe("respond");
  });

  it("hands out a new pass decision each time", async () => {
    const g = gate();
    const first = await g.check(request("/listings", BROWSER));
    (first as { paymentResponse?: string }).paymentResponse = "changed";
    expect(await g.check(request("/listings", BROWSER))).toEqual({ action: "pass" });
  });

  it("starts one background load per refresh, however many requests arrive while it runs", async () => {
    const g = gate();
    await g.check(request("/", BROWSER)); // a person: nothing reported, only the loads run in the background
    clock += 2 * 60 * 60 * 1000; // settings and address lists both due
    const waits: Promise<unknown>[] = [];
    for (let i = 0; i < 5; i++) await g.check(request("/", BROWSER), (p) => waits.push(p));
    await Promise.all(waits);
    expect(waits).toHaveLength(2); // one for the settings, one for the address lists
  });

  it("takes an address index from another bundle (each CommonJS entry has its own NetworkIndex class)", async () => {
    const { inNetworks } = await import("../src/detect.js");
    const foreign = { has: () => true } as unknown as Parameters<typeof inNetworks>[1];
    expect(inNetworks("20.15.240.70", foreign)).toBe(true);
  });

  it("reads no environment variable its option already gives (Deno asks permission for each)", async () => {
    const env = process.env;
    const read: string[] = [];
    process.env = new Proxy(env, { get: (target, name) => (typeof name === "string" && name.startsWith("STILEBASE") && read.push(name), Reflect.get(target, name)) });
    try {
      createGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch });
      expect(read).toEqual([]);
      createGate({ fetch: stilebase.fetch, onError: () => undefined });
      expect(read.sort()).toEqual(["STILEBASE_API_URL", "STILEBASE_KEY"]);
    } finally {
      process.env = env;
    }
  });
});
