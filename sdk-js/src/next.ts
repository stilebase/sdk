// Next.js middleware (middleware.ts; proxy.ts from Next 16): `export default gate()`.

import { NextResponse, type NextFetchEvent, type NextRequest } from "next/server.js"; // with the extension: Next has no "exports" map, so plain Node ESM needs it

import { type GateOptions, addressFrom, addressOf, createGate } from "./core.js";

export interface NextGateOptions extends Omit<GateOptions, "free"> {
  /** Not used on Next.js: Next rewrites the URL middleware sees (a trailing ".rsc" stripped, ".." resolved) while it
   * routes the original path, so no path the gate sees can safely be free. Keep paths out of the gate with your
   * middleware's `config.matcher` instead, which Next applies with its own routing. Given anyway, it's ignored (once
   * with a warning). */
  free?: readonly string[];
  /** Read the visitor's address from this header (one your proxy or CDN sets, e.g. "cf-connecting-ip"). Default: on
   * Vercel, the x-real-ip it sets; elsewhere none (agents hiding behind a browser's user agent aren't caught by
   * address then). Never a header anyone can set. */
  ipHeader?: string;
  /** Or work it out yourself. */
  clientIp?: (request: NextRequest) => string | null | undefined;
}

/** Next.js middleware: AI agents pay per request with x402, settled through Stilebase; everyone else passes. */
export function gate(options: NextGateOptions = {}) {
  const { free, ...rest } = options;
  const siteGate = createGate(rest, "next");
  if (free !== undefined && free !== null && (typeof free === "string" || (Array.isArray(free) && free.length > 0))) {
    const message =
      "free isn't used on Next.js (Next routes a different path than middleware sees): every path is gated. Keep " +
      "paths such as /healthz out of the gate with your middleware's config.matcher instead.";
    try {
      if (options.onError) options.onError(new Error(message));
      else console.warn(`[@stilebase/sdk] ${message}`);
    } catch {
      // a failing error handler must not break the site
    }
  }
  const onVercel = Boolean((globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env?.VERCEL);
  return async function stilebaseGate(request: NextRequest, event?: NextFetchEvent): Promise<NextResponse> {
    let ip: string | null | undefined = null;
    try {
      ip = options.clientIp ? addressOf(options.clientIp(request)) : addressFrom(request.headers, options.ipHeader ?? (onVercel ? "x-real-ip" : undefined), null);
    } catch {
      ip = null; // a clientIp that throws: no address (agents are still asked to pay by user agent)
    }
    const decision = await siteGate.check(
      { method: request.method, url: request.url, headers: request.headers, ip: ip ?? null },
      event ? (task) => event.waitUntil(task) : undefined,
    );
    if (decision.action === "respond") return new NextResponse(decision.body, { status: decision.status, headers: decision.headers });
    const response = NextResponse.next();
    if (decision.paymentResponse) {
      try {
        response.headers.set("PAYMENT-RESPONSE", decision.paymentResponse);
      } catch {
        // the payment settled: serve the page the agent paid for, even without its receipt header
      }
    }
    return response;
  };
}

export type { GateDecision, GateOptions } from "./core.js";
