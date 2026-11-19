// A stand-in for Stilebase's gate API, answering the calls a gate makes, recording them.

import example from "../../conformance/settings.example.json";
import type { GateRequest, GateSettings } from "../src/protocol.js";

export const GATEWAY_ID = "8f14e45f-ceea-467a-9575-1e2a5e4c8a01";
export const KEY = `sbk_${GATEWAY_ID.replaceAll("-", "")}_${"ab".repeat(32)}`;
export const API = "https://stilebase.test";
export const NOW = 1_792_756_800_000;
export const DECIDE = `/v1/gate/sites/${GATEWAY_ID}/decide`;
export const SETTLE = `/v1/gate/sites/${GATEWAY_ID}/settle`;
export const SETTINGS = `/v1/gate/sites/${GATEWAY_ID}/settings`;
export const CAPTURES = `/v1/gate/sites/${GATEWAY_ID}/captures`;
export const PAYMENT_REQUIRED = { action: "respond", status: 402, headers: { "content-type": "application/json", "payment-required": "e30=" }, body: "{}" };

export interface Call {
  method: string;
  path: string;
  headers: Headers;
  body: Record<string, unknown> | null;
}

type Reply = { status: number; body?: unknown; delayMs?: number } | Error;

export function settings(overrides: Partial<GateSettings> = {}): GateSettings {
  return { ...(structuredClone(example) as GateSettings), ...overrides };
}

export class FakeStilebase {
  calls: Call[] = [];
  settings: Reply = { status: 200, body: settings() };
  decide: (body: Record<string, unknown>) => Reply = (body) => (isAgent(body) ? { status: 200, body: PAYMENT_REQUIRED } : { status: 200, body: { action: "pass", remember: { seconds: 600 } } });
  settle: Reply = { status: 200, body: { action: "pass", response_headers: { "payment-response": "eyJzdWNjZXNzIjp0cnVlfQ==" } } };

  readonly fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input));
    const raw = init?.body ? new TextDecoder().decode(init.body as Uint8Array) : "";
    const body = raw ? (JSON.parse(raw) as Record<string, unknown>) : null;
    this.calls.push({ method: init?.method ?? "GET", path: url.pathname, headers: new Headers(init?.headers), body });
    let reply: Reply;
    if (url.pathname === SETTINGS) reply = this.settings;
    else if (url.pathname === DECIDE) reply = this.decide(body ?? {});
    else if (url.pathname === SETTLE) reply = this.settle;
    else if (url.pathname === CAPTURES) reply = { status: 204 };
    else reply = { status: 404 };
    if (reply instanceof Error) throw reply;
    if (reply.delayMs) {
      const signal = init?.signal;
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(resolve, reply.delayMs);
        signal?.addEventListener("abort", () => {
          clearTimeout(timer);
          reject(new DOMException("timed out", "TimeoutError"));
        });
      });
    }
    const text = reply.status === 204 || reply.body === undefined ? null : typeof reply.body === "string" ? reply.body : JSON.stringify(reply.body);
    return new Response(text, { status: reply.status, headers: { "content-type": "application/json" } });
  };

  to(path: string): Call[] {
    return this.calls.filter((c) => c.path === path);
  }
}

function isAgent(body: Record<string, unknown>): boolean {
  const headers = (body.headers as Array<[string, string]> | undefined) ?? [];
  return headers.some(([n, v]) => n === "user-agent" && /GPTBot|curl/.test(v));
}

/** A request as an adapter hands it to the gate. */
export function request(path: string, headers: Record<string, string> = {}, extra: Partial<GateRequest> = {}): GateRequest {
  return { method: "GET", path, scheme: "https", headers: Object.entries(headers), ip: "203.0.113.7", ...extra };
}

export const GPTBOT = { "user-agent": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)" };
export const BROWSER = { "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36", accept: "text/html" };
export const PAYMENT = "eyJ4NDAyVmVyc2lvbiI6Mn0=";
export const PAID = { ...GPTBOT, "payment-signature": PAYMENT };
