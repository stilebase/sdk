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
  /** For the "trust proxy" setting (see visitorAddress). */
  app?: { get(setting: string): unknown } | undefined;
  socket?: { remoteAddress?: string | undefined } | undefined;
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
   * Express's req.ip, which honours the "trust proxy" setting; with `trust proxy` set to `true`, the last
   * X-Forwarded-For entry instead. Never a header anyone can set. */
  ipHeader?: string;
  /** Or work it out yourself. */
  clientIp?: (req: ExpressRequest) => string | null | undefined;
}

/** Express middleware: AI agents pay per request with x402, settled through Stilebase; everyone else passes. */
export function gate(options: ExpressGateOptions = {}) {
  const siteGate = createGate(options, "express");
  let warned = false;
  const warnTrustAll = () => {
    if (warned) return;
    warned = true;
    const error = new Error(
      `Express's "trust proxy" is true, so req.ip is the first X-Forwarded-For entry, which visitors write: the gate uses ` +
        `the last entry (the one your proxy added) instead. Set "trust proxy" to your proxies (e.g. 1 or "loopback").`,
    );
    if (options.onError) options.onError(error);
    else console.warn(`[@stilebase/sdk] ${error.message}`);
  };
  return function stilebaseGate(req: ExpressRequest, res: ExpressResponse, next: (error?: unknown) => void): void {
    const headers = { get: (name: string) => req.get(name) ?? null };
    const ip = visitorAddress(req, headers, options, warnTrustAll);
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

/** Where the visitor's address comes from. `trust proxy: true` trusts every hop, so Express's req.ip is the left-most
 * X-Forwarded-For entry: the visitor's to write. A spoofed operator address with an agent's user agent would get a free
 * pass or that agent's price, so then the gate takes the last entry, the one the nearest proxy added. */
function visitorAddress(
  req: ExpressRequest,
  headers: { get(name: string): string | null },
  options: ExpressGateOptions,
  warnTrustAll: () => void,
): string | null | undefined {
  if (options.clientIp) return options.clientIp(req);
  if (options.ipHeader) return addressFrom(headers, options.ipHeader, null);
  if (req.app?.get("trust proxy") === true) {
    warnTrustAll();
    return addressFrom(headers, "x-forwarded-for", null) ?? req.socket?.remoteAddress ?? null;
  }
  return req.ip;
}

export type { GateDecision, GateOptions } from "./core.js";
