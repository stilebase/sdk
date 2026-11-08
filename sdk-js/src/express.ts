// Express (4 and 5) middleware: `app.use(gate())`, before your routes.

import { type GateOptions, addressFrom, createGate } from "./core.js";
import { requestUrl } from "./detect.js";

/** The parts of Express's request and response the gate uses (no dependency on Express's types). */
export interface ExpressRequest {
  method: string;
  protocol: string;
  originalUrl: string;
  ip?: string | undefined;
  get(name: string): string | undefined;
}

export interface ExpressResponse {
  headersSent: boolean;
  status(code: number): ExpressResponse;
  set(field: Record<string, string>): ExpressResponse;
  setHeader(name: string, value: string): unknown;
  send(body: string): unknown;
}

export interface ExpressGateOptions extends GateOptions {
  /** Read the visitor's address from this header (one your proxy or CDN sets, e.g. "cf-connecting-ip"). Default:
   * Express's req.ip, which honours the "trust proxy" setting. Never a header anyone can set. */
  ipHeader?: string;
  /** Or work it out yourself. */
  clientIp?: (req: ExpressRequest) => string | null | undefined;
}

/** Express middleware: AI agents pay per request with x402, settled through Stilebase; everyone else passes. */
export function gate(options: ExpressGateOptions = {}) {
  const siteGate = createGate(options, "express");
  return function stilebaseGate(req: ExpressRequest, res: ExpressResponse, next: (error?: unknown) => void): void {
    const headers = { get: (name: string) => req.get(name) ?? null };
    const ip = options.clientIp ? options.clientIp(req) : addressFrom(headers, options.ipHeader, req.ip);
    // Behind a proxy, set "trust proxy" so req.protocol is the visitor's.
    const url = requestUrl(req.protocol, req.get("host"), req.originalUrl);
    siteGate
      .check({ method: req.method, url, headers, ip: ip ?? null })
      .then((decision) => {
        if (decision.action === "respond") {
          res.status(decision.status).set(decision.headers).send(decision.body);
          return;
        }
        if (decision.paymentResponse) res.setHeader("PAYMENT-RESPONSE", decision.paymentResponse);
        next();
      })
      .catch((error: unknown) => {
        // Sending the answer failed partway: the request is answered (or broken), never handed on to the routes too.
        if (res.headersSent) next(error);
        else next();
      });
  };
}

export type { GateDecision, GateOptions } from "./core.js";
