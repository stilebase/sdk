// Express (4 and 5) middleware: `app.use(gate())`, before your routes.

import { type GateOptions, addressFrom, addressOf, createGate } from "./core.js";

/** The parts of Express's request and response the gate uses (no dependency on Express's types). */
export interface ExpressRequest {
  method: string;
  protocol: string;
  originalUrl: string;
  ip?: string | undefined;
  get(name: string): string | undefined;
}

export interface ExpressResponse {
  status(code: number): ExpressResponse;
  set(field: Record<string, string>): ExpressResponse;
  setHeader(name: string, value: string): unknown;
  send(body: string): unknown;
}

export interface ExpressGateOptions extends GateOptions {
  /** Read the visitor's address from this header (one your proxy or CDN sets, e.g. "cf-connecting-ip"). Default:
   * Express's req.ip, which follows your "trust proxy" setting. Never a header anyone can set. */
  ipHeader?: string;
  /** Or work it out yourself. */
  clientIp?: (req: ExpressRequest) => string | null | undefined;
}

/** Express middleware: AI agents pay per request with x402, settled through Stilebase; everyone else passes. */
export function gate(options: ExpressGateOptions = {}) {
  const siteGate = createGate(options, "express");
  return function stilebaseGate(req: ExpressRequest, res: ExpressResponse, next: (error?: unknown) => void): void {
    const headers = { get: (name: string) => req.get(name) ?? null };
    let ip: string | null | undefined = null;
    try {
      ip = options.clientIp ? addressOf(options.clientIp(req)) : options.ipHeader ? addressFrom(headers, options.ipHeader) : req.ip;
    } catch {
      ip = null; // a clientIp that throws: no address (agents are still asked to pay by user agent)
    }
    // The path as Express routes it, as sent ("//premium/x" stays that); an absolute-form target (`GET
    // http://host:99999/premium`) keeps what follows its authority, as Express's url.parse reads it, whatever the host.
    // The host doesn't matter: the gate names the page by the gateway's domain.
    const original = typeof req.originalUrl === "string" ? req.originalUrl : "/";
    const rest = original.startsWith("/") ? original : (/^[a-z][a-z0-9+.-]*:\/\/[^/?#]*(.*)$/is.exec(original)?.[1] ?? original);
    const url = `${req.protocol === "https" ? "https" : "http"}://localhost${rest.startsWith("/") ? rest : `/${rest}`}`;
    siteGate.check({ method: req.method, url, headers, ip: ip ?? null }).then(
      (decision) => {
        if (decision.action === "respond") {
          try {
            res.status(decision.status).set(decision.headers).send(decision.body);
          } catch (error) {
            next(error); // the gate's own answer couldn't be sent: an error, never the page
          }
          return;
        }
        if (decision.paymentResponse) {
          try {
            res.setHeader("PAYMENT-RESPONSE", decision.paymentResponse);
          } catch {
            // the payment settled: serve the page the agent paid for, even without its receipt header
          }
        }
        next();
      },
      (error: unknown) => next(error),
    );
  };
}

export type { GateDecision, GateOptions } from "./core.js";
