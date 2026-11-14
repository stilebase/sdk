import { beforeEach, describe, expect, it } from "vitest";

import { type GateOptions, createGate } from "../src/core.js";
import { passToken } from "../src/signing.js";
import { API, BROWSER, CHROME_UA, DOMAIN, FakeStilebase, GATEWAY_ID, GPTBOT, KEY, NOW, REQUIRED, liveConfig, request } from "./fake-stilebase.js";

let stilebase: FakeStilebase;
let clock: number;
let errors: Error[];

function gate(options: GateOptions = {}) {
  return createGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, now: () => clock, onError: (e) => errors.push(e), ...options }, "test");
}

/** Lets background reports and refreshes finish. */
const settled = () => new Promise((resolve) => setTimeout(resolve, 25));
const CONFIG = `/v1/x402/sites/${GATEWAY_ID}/config`;
const SCRIPT = { "user-agent": CHROME_UA, accept: "*/*" }; // a script posing as Chrome: a suspect
const PAID = { ...GPTBOT, "payment-signature": "eyJ4NDAyVmVyc2lvbiI6Mn0=" };

beforeEach(() => {
  stilebase = new FakeStilebase();
  clock = NOW;
  errors = [];
});

describe("people", () => {
  it("pass, and Stilebase is asked only for the settings and the address lists, once", async () => {
    const g = gate();
    expect(await g.check(request("/listings", BROWSER))).toEqual({ action: "pass" });
    expect(await g.check(request("/listings", BROWSER))).toEqual({ action: "pass" });
    expect(stilebase.calls.map((c) => c.path)).toEqual([CONFIG, "/v1/x402/agent-ranges"]);
  });

  it("all pass when the gate has no key, and it says so", async () => {
    const keyless = createGate({ key: "", fetch: stilebase.fetch, onError: (e) => errors.push(e) });
    expect(await keyless.check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
    expect(errors[0]?.message).toMatch(/STILEBASE_KEY isn't set/);
    expect(stilebase.calls).toHaveLength(0);
  });
});

describe("AI agents", () => {
  it("get the 402 with the price, and the visit is reported", async () => {
    const decision = await gate().check(request("/listings?city=Haifa", GPTBOT));
    expect(decision).toMatchObject({ action: "respond", status: 402 });
    if (decision.action !== "respond") return;
    expect(JSON.parse(decision.body)).toEqual(REQUIRED);
    expect(JSON.parse(atob(decision.headers["PAYMENT-REQUIRED"]!))).toEqual(REQUIRED);
    expect(decision.headers["cache-control"]).toBe("no-store");
    const lookup = stilebase.to("/v1/x402/requirements")[0]!.query;
    expect(Object.fromEntries(lookup)).toEqual({ host: DOMAIN, url: `https://${DOMAIN}/listings?city=Haifa`, ua: GPTBOT["user-agent"], ip: "203.0.113.7" });
    await settled();
    expect(stilebase.to("/v1/x402/visits")[0]?.body).toEqual({
      host: DOMAIN,
      request: { method: "GET", path: "/listings?city=Haifa" },
      agent: { ip: "203.0.113.7", user_agent: GPTBOT["user-agent"], headers: {} },
    });
  });

  it("with a free pass (204) read free, reported as one", async () => {
    stilebase.requirements = () => ({ status: 204 });
    expect(await gate().check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
    await settled();
    expect(stilebase.to("/v1/x402/visits")[0]?.body).toMatchObject({ free_pass: true });
  });

  it("are asked about once per page while Stilebase says the price holds (at most 5 minutes)", async () => {
    const g = gate();
    await g.check(request("/listings", GPTBOT));
    await g.check(request("/listings", GPTBOT));
    expect(stilebase.to("/v1/x402/requirements")).toHaveLength(1);
    clock += 301_000;
    await g.check(request("/listings", GPTBOT));
    expect(stilebase.to("/v1/x402/requirements")).toHaveLength(2);
  });

  it("pass when the price lookup fails or Stilebase can't be reached", async () => {
    stilebase.requirements = () => ({ status: 500 });
    expect(await gate().check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
    stilebase.requirements = () => new Error("connect ECONNREFUSED");
    expect(await gate().check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
  });

  it("send Stilebase a URL and user agent cut to its limits", async () => {
    await gate().check(request(`/${"x".repeat(5000)}`, { "user-agent": `GPTBot/1.0 ${"(".repeat(2000)}` }));
    const lookup = stilebase.to("/v1/x402/requirements")[0]!.query;
    expect(lookup.get("url")!.length).toBeLessThanOrEqual(2048);
    expect(new URLSearchParams({ v: lookup.get("ua")! }).toString().length - 2).toBeLessThanOrEqual(1536);
  });

  it("are named by the gateway's domain, whatever the Host or scheme the app sees", async () => {
    await gate().check(request("/a", GPTBOT, { url: "http://app:3000/a?b=1" }));
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("url")).toBe(`https://${DOMAIN}/a?b=1`);
  });
});

describe("scripts and hidden agents", () => {
  it("scripts with their default user agent, or none, pay, unless let through", async () => {
    expect((await gate().check(request("/", { "user-agent": "curl/8.4.0" }))).action).toBe("respond");
    expect((await gate().check(request("/", {}))).action).toBe("respond");
    expect(await gate({ catchHttpClients: false }).check(request("/", { "user-agent": "curl/8.4.0" }))).toEqual({ action: "pass" });
  });

  it("agents from their operators' addresses, or signing with Web Bot Auth, pay behind a browser's user agent", async () => {
    expect((await gate().check(request("/", BROWSER, { ip: "20.15.240.70" }))).action).toBe("respond");
    expect((await gate().check(request("/", BROWSER, { ip: "::ffff:20.15.240.70" }))).action).toBe("respond");
    expect((await gate().check(request("/", { ...BROWSER, "signature-agent": "https://chatgpt.com" }))).action).toBe("respond");
    expect(await gate({ catchHiddenAgents: false }).check(request("/", BROWSER, { ip: "20.15.240.70" }))).toEqual({ action: "pass" });
  });

  it("refresh the address lists in the background once they're an hour old", async () => {
    const g = gate();
    await g.check(request("/", BROWSER));
    clock += 3_601_000;
    await g.check(request("/", BROWSER));
    await settled();
    expect(stilebase.to("/v1/x402/agent-ranges")).toHaveLength(2);
  });
});

describe("browser-looking requests that don't act like a browser", () => {
  it("are counted while strict detection is off (204), and pass", async () => {
    stilebase.requirements = () => ({ status: 204 });
    expect(await gate().check(request("/listings", SCRIPT))).toEqual({ action: "pass" });
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("suspect")).toBe("true");
    await settled();
    expect(stilebase.to("/v1/x402/visits")[0]?.body).toMatchObject({ signals: ["no_accept_language", "no_sec_fetch", "chrome_without_client_hints", "accept_any"] });
  });

  it("get a page under strict detection that lets a real browser through, and its cookie lets it pass", async () => {
    const g = gate();
    const decision = await g.check(request("/listings", { ...SCRIPT, accept: "text/html" }));
    expect(decision).toMatchObject({ action: "respond", status: 402 });
    if (decision.action !== "respond") return;
    expect(decision.headers["content-type"]).toMatch(/text\/html/);
    expect(decision.headers["PAYMENT-REQUIRED"]).toBeTruthy();
    const token = await passToken(`sbsec_${"ab".repeat(32)}`, CHROME_UA, NOW / 1000);
    expect(decision.body).toContain(`stilebase_pass=${token}`);
    expect(await g.check(request("/listings", { ...SCRIPT, cookie: `a=1; stilebase_pass=${token}` }))).toEqual({ action: "pass" });
    const forged = `${NOW / 1000}.${"0".repeat(32)}`;
    expect((await g.check(request("/listings", { ...SCRIPT, cookie: `stilebase_pass=${forged}` }))).action).toBe("respond");
    clock += 43_201_000; // older than max_age_seconds
    expect((await g.check(request("/listings", { ...SCRIPT, cookie: `stilebase_pass=${token}` }))).action).toBe("respond");
  });

  it("are never checked over plain http, or with the checks off", async () => {
    expect(await gate().check(request("/", SCRIPT, { url: `http://${DOMAIN}/` }))).toEqual({ action: "pass" });
    expect(await gate({ checkBrowsers: false }).check(request("/", SCRIPT))).toEqual({ action: "pass" });
  });
});

describe("payments", () => {
  it("settled through Stilebase let the request through, with PAYMENT-RESPONSE", async () => {
    expect(await gate().check(request("/listings", PAID))).toEqual({ action: "pass", paymentResponse: "eyJzdWNjZXNzIjp0cnVlfQ==" });
    const [settle] = stilebase.to("/v1/x402/settle");
    expect(settle?.body).toMatchObject({ host: DOMAIN, payment: PAID["payment-signature"], request: { method: "GET", path: "/listings" } });
    expect(settle?.headers.get("stilebase-signature")).toMatch(/^v2=[0-9a-f]{64}$/);
  });

  it("x402 v1's X-PAYMENT too; an empty PAYMENT-SIGNATURE is no payment", async () => {
    expect((await gate().check(request("/", { ...GPTBOT, "x-payment": "v1" }))).action).toBe("pass");
    expect((await gate().check(request("/", { ...GPTBOT, "payment-signature": "", "x-payment": "v1" }))).action).toBe("pass");
    expect(stilebase.to("/v1/x402/settle").map((c) => (c.body as { payment: string }).payment)).toEqual(["v1", "v1"]);
  });

  it("refused get the 402 again, with the reason", async () => {
    stilebase.settle = { status: 200, body: { success: false, errorReason: "insufficient_funds", header: "eyJzdWNjZXNzIjpmYWxzZX0=" } };
    const decision = await gate().check(request("/listings", PAID));
    expect(decision).toMatchObject({ action: "respond", status: 402 });
    if (decision.action !== "respond") return;
    expect(JSON.parse(decision.body).error).toBe("insufficient_funds");
    expect(decision.headers["PAYMENT-RESPONSE"]).toBe("eyJzdWNjZXNzIjpmYWxzZX0=");
  });

  it.each([
    ["Stilebase down", new Error("connect ECONNREFUSED")],
    ["a 502", { status: 502 }],
    ["no success in the answer", { status: 200, body: { transaction: "0xabc" } }],
  ])("that Stilebase doesn't confirm (%s) get 503: the page isn't served", async (_name, answer) => {
    stilebase.settle = answer as FakeStilebase["settle"];
    expect(await gate().check(request("/listings", PAID))).toMatchObject({ action: "respond", status: 503, headers: { "retry-after": "5" } });
  });

  it("without a price to meet (204) pass without settling", async () => {
    stilebase.requirements = () => ({ status: 204 });
    expect(await gate().check(request("/listings", PAID))).toEqual({ action: "pass" });
    expect(stilebase.to("/v1/x402/settle")).toHaveLength(0);
  });
});

describe("settings", () => {
  it("are fetched signed, saying which package this is", async () => {
    await gate().check(request("/", BROWSER));
    const [call] = stilebase.calls;
    expect(call?.headers.get("stilebase-client")).toMatch(/^@stilebase\/sdk\/\d+\.\d+\.\d+ gate\/test$/);
    expect(call?.headers.get("stilebase-signature")).toMatch(/^v2=[0-9a-f]{64}$/);
  });

  it("serve the verification file, even before the gateway is live", async () => {
    const verification = { path: "/.well-known/stilebase-verify.txt", value: "stilebase-verify=tok" };
    stilebase.config = { status: 200, body: liveConfig({ gateway: { id: GATEWAY_ID, domain: DOMAIN, live: false, reason: "domain_not_verified" }, verification }) };
    const g = gate();
    expect(await g.check(request(verification.path))).toMatchObject({ action: "respond", status: 200, body: "stilebase-verify=tok\n" });
    expect(await g.check(request("/listings", GPTBOT))).toEqual({ action: "pass" }); // not live: nobody is asked
  });

  it("are refreshed in the background, the last good ones kept on failure", async () => {
    const g = gate();
    await g.check(request("/", BROWSER));
    stilebase.config = { status: 500 };
    clock += 301_000;
    await g.check(request("/", BROWSER));
    await settled();
    expect(stilebase.to(CONFIG)).toHaveLength(2);
    expect((await g.check(request("/listings", GPTBOT))).action).toBe("respond"); // still gated
  });

  it("are dropped once the gateway is gone (404)", async () => {
    const g = gate();
    await g.check(request("/", BROWSER));
    stilebase.config = { status: 404 };
    clock += 301_000;
    await g.check(request("/", BROWSER));
    await settled();
    expect(await g.check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
  });

  it("when Stilebase is down at start, everyone passes and it's tried again in 30 seconds", async () => {
    stilebase.config = new Error("connect ECONNREFUSED");
    const g = gate();
    expect(await g.check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
    stilebase.config = { status: 200, body: liveConfig() };
    clock += 30_000;
    await g.check(request("/listings", GPTBOT));
    await settled();
    expect((await g.check(request("/listings", GPTBOT))).action).toBe("respond");
  });

  it("never hold a request past its own deadline", async () => {
    const g = createGate({ key: KEY, apiUrl: API, fetch: () => new Promise<Response>(() => undefined), onError: () => undefined });
    const started = Date.now();
    expect(await g.check(request("/listings", GPTBOT))).toEqual({ action: "pass" });
    expect(Date.now() - started).toBeLessThan(5_000);
  }, 10_000);
});

describe("the review's cases (cycle 1)", () => {
  it("start another load when the first never ends (a runtime cancelled the request that started it)", async () => {
    let calls = 0;
    const hanging = (input: RequestInfo | URL, init?: RequestInit) => (calls++ === 0 ? new Promise<Response>(() => undefined) : stilebase.fetch(input, init));
    const g = createGate({ key: KEY, apiUrl: API, fetch: hanging, now: () => clock, onError: () => undefined });
    const waits: Promise<unknown>[] = [];
    expect(await g.check(request("/listings", GPTBOT), (task) => waits.push(task))).toEqual({ action: "pass" }); // waits 3.5 s
    expect(waits).toHaveLength(1); // the first load, kept alive by the runtime
    clock += 3_600; // past its own timeout: another load starts
    expect((await g.check(request("/listings", GPTBOT))).action).toBe("respond");
  }, 10_000);

  it("never follow a redirect from the API: it's an answer like any other", async () => {
    const seen: (RequestInit | undefined)[] = [];
    const g = createGate({ key: KEY, apiUrl: API, fetch: (input, init) => (seen.push(init), stilebase.fetch(input, init)), now: () => clock });
    await g.check(request("/listings", GPTBOT));
    expect(seen.every((init) => init?.redirect === "manual")).toBe(true);
    stilebase.requirements = () => ({ status: 301, headers: { location: "https://elsewhere.example/" } });
    expect(await gate().check(request("/other", GPTBOT))).toEqual({ action: "pass" });
  });

  it("keep the last address lists when an answer has none", async () => {
    const g = gate();
    await g.check(request("/", BROWSER, { ip: "20.15.240.70" }));
    stilebase.ranges = { status: 200, body: { nothing: true } };
    clock += 3_601_000;
    await g.check(request("/", BROWSER, { ip: "20.15.240.70" }));
    await settled();
    expect((await g.check(request("/", BROWSER, { ip: "20.15.240.70" }))).action).toBe("respond");
  });

  it("name a path that starts with two slashes as it is, never reading its first segment as a host", async () => {
    await gate().check(request("/", GPTBOT, { url: `https://${DOMAIN}//premium/article.html` }));
    expect(stilebase.to("/v1/x402/requirements")[0]?.query.get("url")).toBe(`https://${DOMAIN}//premium/article.html`);
  });
});
