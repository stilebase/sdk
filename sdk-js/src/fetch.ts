// Any runtime with the Fetch API's Request and Response: Cloudflare Workers, Hono, Bun, Deno.
//   export default { fetch: withGate(handler) }       (Workers: STILEBASE_KEY as a secret, read from env)
//   Deno.serve(withGate(handler))

import { type Gate, type GateOptions, addressFrom, createGate } from "./core.js";

export interface FetchGateOptions extends GateOptions {
  /** Read the visitor's address from this header (one your proxy or CDN sets). Default: on Cloudflare,
   * CF-Connecting-IP; elsewhere none. Never a header anyone can set. */
  ipHeader?: string;
  /** Or work it out yourself (Bun: `server.requestIP(request)?.address`; Deno: the handler's info.remoteAddr.hostname). It must
   * return a string (or null): anything else counts as no address. */
  clientIp?: (request: Request, ...rest: unknown[]) => string | null | undefined;
}

type Handler<Rest extends unknown[]> = (request: Request, ...rest: Rest) => Response | Promise<Response>;

interface CloudflareRequest extends Request {
  cf?: { httpProtocol?: string; asn?: number };
}

/** Wraps a fetch handler: AI agents pay per request with x402, settled through Stilebase; everyone else reaches it. */
export function withGate<Rest extends unknown[]>(handler: Handler<Rest>, options: FetchGateOptions = {}): Handler<Rest> {
  let siteGate: Gate | null = null;
  return async (request: Request, ...rest: Rest): Promise<Response> => {
    // Workers have no process.env: the key and the API come from the Worker's env (the second argument) on first use.
    siteGate ??= createGate(
      { ...options, key: options.key ?? fromEnv(rest[0], "STILEBASE_KEY"), apiUrl: options.apiUrl ?? fromEnv(rest[0], "STILEBASE_API_URL") },
      "fetch",
    );
    const cf = (request as CloudflareRequest).cf;
    let ip: string | null | undefined = null;
    try {
      ip = options.clientIp
        ? options.clientIp(request, ...rest)
        : addressFrom(request.headers, options.ipHeader ?? (cf ? "cf-connecting-ip" : undefined), null);
    } catch {
      ip = null; // a clientIp that throws: no address (agents are still asked to pay by user agent)
    }
    const ctx = rest[1] as { waitUntil?: (task: Promise<unknown>) => void } | undefined;
    const waitUntil = typeof ctx?.waitUntil === "function" ? (task: Promise<unknown>) => ctx.waitUntil!(task) : undefined;
    const decision = await siteGate.check(
      { method: request.method, url: request.url, headers: request.headers, ip: ip ?? null, httpProtocol: cf?.httpProtocol ?? null, asn: cf?.asn ?? null },
      waitUntil,
    );
    if (decision.action === "respond") return new Response(decision.body, { status: decision.status, headers: decision.headers });
    const response = await handler(request, ...rest);
    if (!decision.paymentResponse) return response;
    try {
      const paid = new Response(response.body, response); // a fresh Response: the handler's headers may be immutable
      paid.headers.set("PAYMENT-RESPONSE", decision.paymentResponse);
      return paid;
    } catch {
      return response; // the payment settled: serve the page the agent paid for, even without its receipt header
    }
  };
}

function fromEnv(env: unknown, name: string): string | undefined {
  const value = (env as Record<string, unknown> | undefined)?.[name];
  return typeof value === "string" ? value : undefined;
}

export type { GateDecision, GateOptions } from "./core.js";
