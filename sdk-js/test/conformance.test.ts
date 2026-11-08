// conformance/vectors.json: what Stilebase's server, its Cloudflare Worker and both SDKs must agree on.

import { describe, expect, it } from "vitest";

import example from "../../conformance/gate-config.example.json";
import vectors from "../../conformance/vectors.json";
import { type GateConfig, inNetworks, isAiAgent, isScript, normalizeIp, parseNetwork, suspectSignals } from "../src/detect.js";
import { parseSiteKey, passToken, signedHeaders } from "../src/signing.js";
import { VERSION } from "../src/version.js";
import pkg from "../package.json";
import { createGate } from "../src/core.js";
import { API, DOMAIN, FakeStilebase, GPTBOT, KEY, request } from "./fake-stilebase.js";

const config = example as GateConfig;

describe("conformance vectors", () => {
  it.each(vectors.signing)("signs $method $path like the server", async (v) => {
    const headers = await signedHeaders(v.secret, v.method, v.path, new TextEncoder().encode(v.body), v.timestamp * 1000);
    expect(headers).toEqual(v.headers);
  });

  it("reads site keys", () => {
    for (const v of vectors.site_keys.valid) expect(parseSiteKey(v.key)).toEqual({ gatewayId: v.gateway_id, secret: v.secret });
    for (const key of vectors.site_keys.invalid) expect(parseSiteKey(key)).toBeNull();
  });

  it.each(vectors.pass_tokens)("makes the browser check's pass token", async (v) => {
    expect(await passToken(v.secret, v.user_agent, v.timestamp)).toBe(v.token);
  });

  it.each(vectors.agent_matching.cases)("tells agents and scripts apart: $user_agent", (v) => {
    expect(isAiAgent(v.user_agent, config)).toBe(v.agent);
    expect(isAiAgent(v.user_agent, config, true)).toBe(v.test_mode);
    expect(isScript(v.user_agent, config)).toBe(v.script);
  });

  it.each(vectors.suspect_signals.cases)("scores browser-looking requests: $name", (v) => {
    const request = { method: v.method, url: v.url, headers: new Headers({ ...(v.headers as Record<string, string>), "user-agent": v.user_agent }), httpProtocol: v.http_protocol, asn: v.asn };
    expect(suspectSignals(request, config)).toEqual(v.signals);
  });

  it("matches addresses against the operators' ranges", () => {
    const networks = vectors.ip_ranges.prefixes.map(parseNetwork).filter((n) => n !== null);
    expect(networks).toHaveLength(vectors.ip_ranges.prefixes.length);
    for (const v of vectors.ip_ranges.cases) expect(inNetworks(v.ip, networks), v.ip).toBe(v.in);
  });

  it.each(vectors.client_ips.cases)("normalizes the visitor's address: $ip", (v) => {
    expect(normalizeIp(v.ip)).toBe(v.normalized);
  });

  it("reports the version in package.json", () => {
    expect(VERSION).toBe(pkg.version);
  });
});

describe("the review's cases (cycle 1)", () => {
  it("looks addresses up in an index that agrees with the plain list", async () => {
    const { NetworkIndex } = await import("../src/detect.js");
    const networks = vectors.ip_ranges.prefixes.map(parseNetwork).filter((n) => n !== null);
    const index = new NetworkIndex(networks);
    for (const v of vectors.ip_ranges.cases) expect(inNetworks(v.ip, index), v.ip).toBe(v.in);
  });

  it("builds the request URL without letting the Host header move the path", async () => {
    const { requestUrl } = await import("../src/detect.js");
    expect(requestUrl("https", "shop.example.com", "/premium/x?a=1")).toBe("https://shop.example.com/premium/x?a=1");
    expect(new URL(requestUrl("https", "shop.example.com?", "/premium/x")).pathname).toBe("/premium/x");
    expect(new URL(requestUrl("https", "a.example#", "/premium/x")).pathname).toBe("/premium/x");
    expect(requestUrl("http", "[::1]:8080", "/")).toBe("http://[::1]:8080/");
    expect(requestUrl("https", "shop.example.com", "http://evil.example/premium/x?b")).toBe("https://shop.example.com/premium/x?b");
    expect(new URL(requestUrl("https", "shop.example.com", "/premium/a#b")).pathname).toBe("/premium/a%23b");
  });
});

describe("conformance vectors: the gate's decisions", () => {
  it.each(vectors.paths.cases)("matches paths: $prefixes against $path", async (v) => {
    const stilebase = new FakeStilebase();
    const gate = createGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, paths: v.prefixes, onError: () => undefined });
    const decision = await gate.check(request(v.path, GPTBOT, { url: `https://${DOMAIN}${v.path}` }));
    expect(decision.action === "respond").toBe(v.gated);
  });

  it.each(vectors.payment_headers.cases)("takes the payment from $headers", async (v) => {
    const stilebase = new FakeStilebase();
    const gate = createGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, onError: () => undefined });
    await gate.check(request("/listings/1", { ...GPTBOT, ...(v.headers as Record<string, string>) }));
    const settled = stilebase.to("/v1/x402/settle")[0]?.body as { payment?: string } | undefined;
    expect(settled?.payment ?? null).toBe(v.payment);
  });
});
