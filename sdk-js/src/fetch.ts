// Any runtime with the Fetch API's Request and Response: Cloudflare Workers, Hono, Bun, Deno.
//   export default { fetch: withGate(handler) }       (Workers: STILEBASE_KEY as a secret, read from env)
//   Deno.serve(withGate(handler))

import { type Gate, type GateOptions, addressFrom, addressOf, createGate } from "./core.js";

export interface FetchGateOptions<Rest extends unknown[] = unknown[]> extends GateOptions {
  /** Read the visitor's address from this header (one your proxy or CDN sets). Default: on Cloudflare,
   * CF-Connecting-IP; elsewhere none. Never a header anyone can set. */
  ipHeader?: string;
  /** Or work it out yourself, from the request and the handler's other arguments (Bun: `server.requestIP(request)?.address`;
   * Deno: `info.remoteAddr.hostname`). It must return a string, synchronously (or null): anything else is no address. */
  clientIp?: (request: Request, ...rest: Rest) => string | null | undefined;
}

type Handler<Rest extends unknown[]> = (request: Request, ...rest: Rest) => Response | Promise<Response>;

interface CloudflareRequest extends Request {
  cf?: { httpProtocol?: string; asn?: number };
}

/** Wraps a fetch handler: AI agents pay per request with x402, settled through Stilebase; everyone else reaches it. */
export function withGate<Rest extends unknown[]>(handler: Handler<Rest>, options: FetchGateOptions<Rest> = {}): Handler<Rest> {
  let siteGate: Gate | null = null;
  let startFailed = false;
  return async (request: Request, ...rest: Rest): Promise<Response> => {
    // Workers have no process.env: the key and the API come from the Worker's env (the second argument) on first use.
    try {
      siteGate ??= createGate(
        { ...options, key: options.key ?? fromEnv(rest[0], "STILEBASE_KEY"), apiUrl: options.apiUrl ?? fromEnv(rest[0], "STILEBASE_API_URL") },
        "fetch",
      );
    } catch (error) {
      // The gate couldn't start (e.g. no permission to read the environment): never break the site, and say so once.
      if (!startFailed) {
        startFailed = true;
        const message = `[@stilebase/sdk] the gate couldn't start, so every request passes: ${error instanceof Error ? error.message : String(error)}`;
        try {
          if (options.onError) options.onError(new Error(message));
          else console.warn(message);
        } catch {
          // a failing error handler must not break the request
        }
      }
      return handler(request, ...rest);
    }
    const cf = (request as CloudflareRequest).cf;
    let ip: string | null | undefined = null;
    try {
      ip = options.clientIp
        ? addressOf(options.clientIp(request, ...rest))
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
