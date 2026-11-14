// conformance/vectors.json: what Stilebase's server, its Cloudflare Worker and both SDKs must agree on.

import { describe, expect, it } from "vitest";

import example from "../../conformance/gate-config.example.json";
import vectors from "../../conformance/vectors.json";
import pkg from "../package.json";
import { type GateConfig, inNetworks, isAiAgent, isScript, normalizeIp, parseConfig, parseNetwork, suspectSignals } from "../src/detect.js";
import { parseSiteKey, passToken, signedHeaders } from "../src/signing.js";
import { VERSION } from "../src/version.js";

const config = parseConfig(example) as GateConfig;

describe("conformance vectors", () => {
  it("reads the example settings", () => {
    expect(config).not.toBeNull();
  });

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
    expect(isScript(v.user_agent, config)).toBe(v.script);
  });

  it.each(vectors.suspect_signals.cases)("scores browser-looking requests: $name", (v) => {
    const headers = new Headers({ ...(v.headers as Record<string, string>), "user-agent": v.user_agent });
    expect(suspectSignals({ method: v.method, url: v.url, headers, httpProtocol: v.http_protocol, asn: v.asn }, config)).toEqual(v.signals);
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
