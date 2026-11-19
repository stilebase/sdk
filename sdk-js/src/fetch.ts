// Any runtime with the Fetch API's Request and Response: Cloudflare Workers, Hono, Bun, Deno.
//   export default { fetch: withGate(handler) }       (Workers: STILEBASE_KEY as a secret, read from env)
//   Deno.serve(withGate(handler))

import { type Gate, type GateOptions, type WaitUntil, addressFrom, addressOf, createGate } from "./core.js";
import type { Capture, GateRequest } from "./protocol.js";
import { normalizeIp } from "./protocol.js";

export interface FetchGateOptions<Rest extends unknown[] = unknown[]> extends GateOptions {
  /** Read the visitor's address from this header (one your proxy or CDN sets). Default: on Cloudflare,
   * CF-Connecting-IP; elsewhere none. Never a header anyone can set. */
  ipHeader?: string;
  /** Or work it out yourself, from the request and the handler's other arguments (Bun: `server.requestIP(request)?.address`;
   * Deno: `info.remoteAddr.hostname`). */
  clientIp?: (request: Request, ...rest: Rest) => string | null | undefined;
}

type Handler<Rest extends unknown[]> = (request: Request, ...rest: Rest) => Response | Promise<Response>;

interface CloudflareRequest extends Request {
  cf?: { httpProtocol?: string; asn?: number };
}

/** Wraps a fetch handler: Stilebase decides each request; a payment is settled once the handler answered. */
export function withGate<Rest extends unknown[]>(handler: Handler<Rest>, options: FetchGateOptions<Rest> = {}): Handler<Rest> {
  let siteGate: Gate | null = null;
  return async (request: Request, ...rest: Rest): Promise<Response> => {
    // Workers have no process.env: the key, the API and the switch come from the Worker's env on first use.
    siteGate ??= createGate(
      {
        ...options,
        key: options.key ?? fromEnv(rest[0], "STILEBASE_KEY"),
        apiUrl: options.apiUrl ?? fromEnv(rest[0], "STILEBASE_API_URL"),
        disabled: options.disabled ?? (fromEnv(rest[0], "STILEBASE_DISABLED") === "1" ? true : undefined),
      },
      "fetch",
    );
    const gate = siteGate;
    const ctx = rest[1] as { waitUntil?: (task: Promise<unknown>) => void } | undefined;
    const waitUntil: WaitUntil | undefined = typeof ctx?.waitUntil === "function" ? (task) => ctx.waitUntil!(task) : undefined;
    const decision = await gate.check(gateRequest(request, options, rest), waitUntil);
    if (decision.action === "respond") return new Response(decision.body, { status: decision.status, headers: decision.headers });

    const started = Date.now();
    const response = await handler(request, ...rest);
    const ttfbMs = Date.now() - started;

    if (decision.action === "hold") {
      const held = await readUpTo(response.body, decision.maxBytes);
      if (held.truncated) return toResponse(gate.tooLarge());
      const settled = await gate.settle(decision.ticket, { status: response.status, body: held.bytes });
      if (settled.action === "respond") return toResponse(settled);
      if (decision.capture) gate.capture(decision.capture, captured(response, held, decision.capture, ttfbMs), waitUntil);
      return rebuilt(response, held.bytes, settled.responseHeaders);
    }

    let served = response;
    if (Object.keys(decision.responseHeaders).length) served = rebuilt(response, response.body, decision.responseHeaders);
    if (!decision.capture || !served.body) return served;
    // A copy as it goes out: the visitor's stream is never held back or changed.
    const [toVisitor, toCopy] = served.body.tee();
    const capture = decision.capture;
    const copying = readUpTo(toCopy, capture.maxBytes, true).then((copy) => gate.capture(capture, captured(served, copy, capture, ttfbMs), waitUntil));
    if (waitUntil) waitUntil(copying.catch(() => undefined));
    else copying.catch(() => undefined);
    return new Response(toVisitor, served);
  };
}

function gateRequest<Rest extends unknown[]>(request: Request, options: FetchGateOptions<Rest>, rest: Rest): GateRequest {
  const cf = (request as CloudflareRequest).cf;
  let ip: string | null = null;
  try {
    const header = options.ipHeader ?? (cf ? "cf-connecting-ip" : undefined);
    ip = options.clientIp ? addressOf(options.clientIp(request, ...rest)) : header ? addressFrom(request.headers.get(header)) : null;
  } catch {
    ip = null; // a clientIp that throws: no address
  }
  let path = "/";
  let scheme = "https";
  try {
    const url = new URL(request.url);
    path = url.pathname + url.search;
    scheme = url.protocol.replace(":", "");
  } catch {
    // a URL that doesn't parse: the root, over https
  }
  return {
    method: request.method,
    path,
    scheme,
    headers: [...request.headers],
    ip: normalizeIp(ip),
    httpProtocol: cf?.httpProtocol ?? null,
    asn: typeof cf?.asn === "number" ? cf.asn : null,
  };
}

/** A body read up to `max` bytes: what was read, the whole size and whether there was more. With `toEnd`, the rest is
 * read (and counted) but not kept; without it, reading stops past `max`. */
async function readUpTo(body: ReadableStream<Uint8Array> | null, max: number, toEnd = false): Promise<{ bytes: Uint8Array; size: number; truncated: boolean }> {
  if (!body) return { bytes: new Uint8Array(), size: 0, truncated: false };
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let kept = 0;
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (kept < max) {
      const part = value.subarray(0, max - kept);
      chunks.push(part);
      kept += part.byteLength;
    }
    if (size > max && !toEnd) {
      reader.cancel().catch(() => undefined);
      break;
    }
  }
  const bytes = new Uint8Array(kept);
  let at = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, at);
    at += chunk.byteLength;
  }
  return { bytes, size, truncated: size > max };
}

function captured(response: Response, copy: { bytes: Uint8Array; size: number; truncated: boolean }, capture: Capture, ttfbMs: number) {
  return {
    status: response.status,
    headers: [...response.headers] as Array<[string, string]>,
    body: copy.bytes.subarray(0, capture.maxBytes),
    size: copy.size,
    truncated: copy.size > capture.maxBytes,
    ttfbMs,
  };
}

/** The response with these headers set (a fresh Response: the handler's headers may be immutable). */
function rebuilt(response: Response, body: BodyInit | null, headers: Record<string, string>): Response {
  const copy = new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
  for (const [name, value] of Object.entries(headers)) {
    try {
      copy.headers.set(name, value);
    } catch {
      // a header the runtime refuses: the page is still served
    }
  }
  return copy;
}

function toResponse(decision: { status: number; headers: Record<string, string>; body: string }): Response {
  return new Response(decision.body, { status: decision.status, headers: decision.headers });
}

function fromEnv(env: unknown, name: string): string | undefined {
  const value = (env as Record<string, unknown> | undefined)?.[name];
  return typeof value === "string" ? value : undefined;
}

export type { GateDecision, GateOptions } from "./core.js";
