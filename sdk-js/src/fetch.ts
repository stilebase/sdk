// Any runtime with the Fetch API's Request and Response: Cloudflare Workers, Hono, Bun, Deno.
//   export default { fetch: withGate(handler) }       (Workers: STILEBASE_KEY as a secret, read from env)
//   Deno.serve(withGate(handler))

import { type Gate, type GateOptions, type WaitUntil, addressFrom, addressOf, createGate, switchedOff } from "./core.js";
import type { Capture, GateRequest } from "./protocol.js";
import { Copy, bodyAllowed, mergeHeaders, normalizeIp } from "./protocol.js";

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
        disabled: options.disabled ?? (switchedOff(fromEnv(rest[0], "STILEBASE_DISABLED")) ? true : undefined),
      },
      "fetch",
    );
    const gate = siteGate;
    const ctx = rest[1] as { waitUntil?: (task: Promise<unknown>) => void } | undefined;
    const waitUntil: WaitUntil | undefined = typeof ctx?.waitUntil === "function" ? (task) => ctx.waitUntil!(task) : undefined;
    const decision = await gate.check(gateRequest(request, options, rest), waitUntil);
    if (decision.action === "respond") return toResponse(decision);

    const started = Date.now();
    const response = await handler(request, ...rest);
    const ttfbMs = Date.now() - started;

    if (decision.action === "hold") {
      // A protocol switch (a WebSocket's 101) can't be held or paid for: not settled.
      if (response.status === 101) return toResponse(gate.tooLarge());
      const held = await readUpTo(response.body, decision.maxBytes);
      if (held.truncated) return toResponse(gate.tooLarge());
      const settled = await gate.settle(decision.ticket, { status: response.status, body: held.bytes });
      if (settled.action === "respond") return toResponse(settled);
      const sent = rebuilt(response, bodyAllowed(response.status) ? held.bytes : null, decision.responseHeaders, settled.responseHeaders);
      if (decision.capture) gate.capture(decision.capture, captured(sent, held, decision.capture, ttfbMs), waitUntil); // as it goes out
      return sent;
    }

    let served = response;
    if (Object.keys(decision.responseHeaders).length && response.status !== 101) served = rebuilt(response, response.body, decision.responseHeaders);
    if (!decision.capture || !served.body) return served;
    // A copy as it goes out, at the visitor's pace: the stream is never held back, changed or buffered beyond the copy.
    const capture = decision.capture;
    const copying = copier(capture.maxBytes, (copy) => gate.capture(capture, captured(served, copy, capture, ttfbMs), waitUntil));
    return new Response(served.body.pipeThrough(copying), served);
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

/** A pass-through stream that keeps the first `max` bytes and counts the rest, then hands over the copy. */
function copier(max: number, done: (copy: { bytes: Uint8Array; size: number; truncated: boolean }) => void): TransformStream<Uint8Array, Uint8Array> {
  const copy = new Copy(max);
  return new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      controller.enqueue(chunk);
      try {
        copy.add(chunk);
      } catch {
        // copying never gets in the visitor's way
      }
    },
    flush() {
      try {
        done({ bytes: copy.bytes(), size: copy.size, truncated: copy.truncated });
      } catch {
        // nor does sending it
      }
    },
  });
}

/** A body read up to `max` bytes: what was read, the whole size so far and whether there was more (reading stops past
 * `max`). */
async function readUpTo(body: ReadableStream<Uint8Array> | null, max: number): Promise<{ bytes: Uint8Array; size: number; truncated: boolean }> {
  const copy = new Copy(max);
  if (body) {
    const reader = body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      copy.add(value);
      if (copy.truncated) {
        reader.cancel().catch(() => undefined);
        break;
      }
    }
  }
  return { bytes: copy.bytes(), size: copy.size, truncated: copy.truncated };
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

/** The response with the overlays' headers replacing same-name ones (SPEC.md section 10; a fresh Response: the
 * handler's headers may be immutable). */
function rebuilt(response: Response, body: BodyInit | null, ...overlays: Array<Record<string, string>>): Response {
  const headers = new Headers();
  for (const [name, value] of mergeHeaders(response.headers, ...overlays)) {
    try {
      headers.append(name, value);
    } catch {
      // a header the runtime refuses: the page is still served
    }
  }
  try {
    return new Response(body, { status: response.status, statusText: response.statusText, headers });
  } catch {
    return new Response(null, { status: response.status, statusText: response.statusText, headers });
  }
}

/** The gate's own answer as a Response. A status or header the runtime refuses (a 1xx, a body on 204/205/304, a header
 * with a newline) never throws into the app: its body is dropped for a null-body status, the header is left out, and
 * a status that can't be sent at all becomes 502. */
function toResponse(decision: { status: number; headers: Record<string, string>; body: string }): Response {
  const status = decision.status >= 200 && decision.status <= 599 ? decision.status : 502;
  const headers = new Headers();
  for (const [name, value] of Object.entries(decision.headers)) {
    try {
      headers.set(name, value);
    } catch {
      // a header the runtime refuses is left out
    }
  }
  const empty = !bodyAllowed(status);
  try {
    return new Response(empty ? null : decision.body, { status, headers });
  } catch {
    return new Response(null, { status: 502, headers: { "cache-control": "no-store" } });
  }
}

function fromEnv(env: unknown, name: string): string | undefined {
  const value = (env as Record<string, unknown> | undefined)?.[name];
  return typeof value === "string" ? value : undefined;
}

export type { GateDecision, GateOptions } from "./core.js";
