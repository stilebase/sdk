// The shared cases (conformance/vectors.json): every gate, in any language, must agree with each of them.

import { describe, expect, it } from "vitest";

import vectors from "../../conformance/vectors.json";
import settingsExample from "../../conformance/settings.example.json";
import { createGate } from "../src/core.js";
import {
  type GateRequest,
  type GateSettings,
  bodyAllowed,
  captureHeaders,
  decideBody,
  mergeHeaders,
  normalizeIp,
  notPayableAnswer,
  parseSettings,
  readAnswer,
  rememberMaterial,
  skipped,
  unknownPaymentAnswer,
} from "../src/protocol.js";
import { parseSiteKey, sha256Hex, signedHeaders } from "../src/signing.js";
import { API, FakeStilebase, KEY, NOW, PAYMENT, request } from "./fake-stilebase.js";

const settings = parseSettings(settingsExample) as GateSettings;

describe("conformance", () => {
  it.each(vectors.signing)("signs $method $path", async (v) => {
    expect(await signedHeaders(v.secret, v.method, v.path, new TextEncoder().encode(v.body), v.timestamp * 1000)).toEqual(v.headers);
  });

  it("reads site keys", () => {
    for (const v of vectors.site_keys.valid) expect(parseSiteKey(v.key)).toEqual({ gatewayId: v.gateway_id, secret: v.secret });
    for (const key of vectors.site_keys.invalid) expect(parseSiteKey(key)).toBeNull();
  });

  it.each(vectors.client_ips.cases)("normalizes $ip", (v) => {
    expect(normalizeIp(v.ip)).toBe(v.normalized);
  });

  it.each(vectors.remember_keys.cases)("remembers $path", async (v) => {
    const r: GateRequest = { method: "GET", path: v.path, scheme: "https", headers: v.user_agent ? [["User-Agent", v.user_agent]] : [], ip: v.client_ip };
    expect(await sha256Hex(rememberMaterial(r, settings.remember_by))).toBe(v.key);
  });

  it.each(vectors.skip_paths.cases)("skip $path: $skip ($why)", (v) => {
    expect(skipped(v.path, vectors.skip_paths.skip_paths)).toBe(v.skip);
  });

  it.each(vectors.decide_bodies.cases)("decide body: $name", (v) => {
    const r = v.request as { method: string; scheme: string; path: string; assume_https: boolean; client_ip: string | null; headers: Array<[string, string]>; edge?: { asn: number; http_protocol: string } };
    const req: GateRequest = { method: r.method, path: r.path, scheme: r.scheme, headers: r.headers, ip: r.client_ip, asn: r.edge?.asn ?? null, httpProtocol: r.edge?.http_protocol ?? null };
    expect(decideBody(req, settings, r.assume_https)).toEqual(v.body);
  });

  it.each(vectors.settings.cases)("settings: $name", (v) => {
    expect(parseSettings(v.body)).toEqual(v.read);
  });

  it.each(vectors.answers.cases)("answer: $name", async (v) => {
    for (const paying of [false, true]) {
      const stilebase = new FakeStilebase();
      stilebase.decide = () => ({ status: v.http_status, body: v.body });
      const gate = createGate({ key: KEY, apiUrl: API, fetch: stilebase.fetch, now: () => NOW });
      const decision = await gate.check(request("/listings", paying ? { "payment-signature": PAYMENT } : {}));
      const expected = paying ? v.with_payment : v.no_payment;
      const unknown = decision.action === "respond" && decision.status === 503 && decision.body.includes("payment_outcome_unknown");
      const actual = decision.action === "hold" ? "settle_after" : unknown ? "unknown_payment" : decision.action;
      expect(actual).toBe(expected);
      if ("response_headers" in v && (decision.action === "pass" || decision.action === "hold")) expect(decision.responseHeaders).toEqual(v.response_headers);
      if ("respond" in v && decision.action === "respond") expect({ status: decision.status, body: decision.body }).toEqual(v.respond);
      if ("retry_after" in v && decision.action === "respond") expect(decision.headers["retry-after"]).toBe(String(v.retry_after));
      if ("capture" in v && decision.action === "pass") expect(decision.capture).toEqual(v.capture ? { token: v.capture.token, maxBytes: v.capture.max_bytes } : null);
    }
    if ("remember_seconds" in v) {
      const answer = readAnswer(v.http_status, v.body);
      expect(answer.kind === "pass" ? answer.rememberSeconds : null).toBe(v.remember_seconds);
    }
  });

  describe("answers sent", () => {
    it.each(vectors.answers_sent.merges)("merges headers %#", (v) => {
      expect(mergeHeaders(v.site as Array<[string, string]>, ...(v.overlays as Array<Record<string, string>>))).toEqual(v.sent);
    });
    it.each(vectors.answers_sent.body_allowed)("a body with $status: $allowed", (v) => {
      expect(bodyAllowed(v.status)).toBe(v.allowed);
    });
    it("keeps no cookies in a capture", () => {
      for (const v of vectors.answers_sent.capture_headers) expect(captureHeaders(v.headers as Array<[string, string]>)).toEqual(v.kept);
    });
    it("sends the gate's own answers exactly", () => {
      const own = vectors.answers_sent.gate_answers;
      expect(unknownPaymentAnswer(own.unknown_payment.retry_after)).toEqual({ status: 503, headers: own.unknown_payment.headers, body: own.unknown_payment.body });
      expect(notPayableAnswer()).toEqual({ status: 502, headers: own.not_payable.headers, body: own.not_payable.body });
    });
  });
});
