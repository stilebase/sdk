// A stand-in for Stilebase's x402 API, answering the calls a gate makes, recording them.

import example from "../../conformance/gate-config.example.json";
import type { GateConfig } from "../src/detect.js";

export const GATEWAY_ID = "8f14e45f-ceea-467a-9575-1e2a5e4c8a01";
export const KEY = `sbk_${GATEWAY_ID.replaceAll("-", "")}_${"ab".repeat(32)}`;
export const API = "https://stilebase.test";
export const DOMAIN = "shop.example.com";
export const NOW = 1_792_756_800_000;
export const REQUIRED = {
  x402Version: 2,
  error: "PAYMENT-SIGNATURE header is required",
  resource: { url: `https://${DOMAIN}/listings` },
  accepts: [{ scheme: "exact", network: "eip155:8453", amount: "10000", payTo: "0xPlatform" }],
};

export interface Call {
  method: string;
  path: string;
  query: URLSearchParams;
  headers: Headers;
  body: unknown;
}

export function liveConfig(overrides: Partial<GateConfig> = {}): GateConfig {
  const config = structuredClone(example) as GateConfig;
  return { ...config, gateway: { ...config.gateway, domain: DOMAIN, live: true, reason: null }, verification: null, ...overrides };
}

type Answer = { status: number; body?: unknown; headers?: Record<string, string> } | Error;

export class FakeStilebase {
  calls: Call[] = [];
  config: Answer = { status: 200, body: liveConfig() };
  requirements: (query: URLSearchParams) => Answer = () => ({ status: 200, body: REQUIRED, headers: { "cache-control": "public, max-age=300" } });
  settle: Answer = { status: 200, body: { success: true, transaction: "0xabc", network: "eip155:8453", header: "eyJzdWNjZXNzIjp0cnVlfQ==" } };
  ranges: Answer = { status: 200, body: { prefixes: ["20.15.240.64/28", "2600:1f28:365:80b0::/60"] } };

  readonly fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input));
    const raw = init?.body ? new TextDecoder().decode(init.body as Uint8Array) : "";
    const call: Call = { method: init?.method ?? "GET", path: url.pathname, query: url.searchParams, headers: new Headers(init?.headers), body: raw ? JSON.parse(raw) : null };
    this.calls.push(call);
    let answer: Answer;
    if (url.pathname === `/v1/x402/sites/${GATEWAY_ID}/config`) answer = this.config;
    else if (url.pathname === "/v1/x402/requirements") answer = this.requirements(url.searchParams);
    else if (url.pathname === "/v1/x402/agent-ranges") answer = this.ranges;
    else if (url.pathname === "/v1/x402/settle") answer = this.settle;
    else if (url.pathname === "/v1/x402/visits") answer = { status: 204 };
    else answer = { status: 404 };
    if (answer instanceof Error) throw answer;
    const body = answer.status === 204 || answer.body === undefined ? null : JSON.stringify(answer.body);
    return new Response(body, { status: answer.status, headers: { "content-type": "application/json", ...answer.headers } });
  };

  to(path: string): Call[] {
    return this.calls.filter((c) => c.path === path);
  }
}

/** A request as an adapter hands it to the gate. */
export function request(path: string, headers: Record<string, string> = {}, extra: { method?: string; ip?: string; url?: string } = {}) {
  return {
    method: extra.method ?? "GET",
    url: extra.url ?? `https://${DOMAIN}${path}`,
    headers: new Headers(headers),
    ip: extra.ip ?? "203.0.113.7",
  };
}

export const GPTBOT = { "user-agent": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)" };
export const CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";
export const BROWSER = {
  "user-agent": CHROME_UA,
  accept: "text/html,application/xhtml+xml",
  "accept-language": "en-US,en;q=0.9",
  "sec-fetch-mode": "navigate",
  "sec-fetch-site": "none",
  "sec-fetch-dest": "document",
  "sec-ch-ua": '"Chromium";v="130"',
};
