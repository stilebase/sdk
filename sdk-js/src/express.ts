// Express (4 and 5) middleware: `app.use(gate())`, before your routes.

import { type GateOptions, addressFrom, addressOf, createGate } from "./core.js";
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
   * Express's req.ip, which honours the "trust proxy" setting; when that trusts every hop (`true`, or ranges or a
   * function that trust any address), the last X-Forwarded-For entry instead. Never a header anyone can set. */
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
      `Express's "trust proxy" trusts every hop, so req.ip is the first X-Forwarded-For entry, which visitors write: the gate uses ` +
        `the last entry (the one your proxy added) instead. Set "trust proxy" to your proxies (e.g. 1 or "loopback").`,
    );
    try {
      if (options.onError) options.onError(error);
      else console.warn(`[@stilebase/sdk] ${error.message}`);
    } catch {
      // a failing error handler must not break the request
    }
  };
  return function stilebaseGate(req: ExpressRequest, res: ExpressResponse, next: (error?: unknown) => void): void {
    const headers = { get: (name: string) => req.get(name) ?? null };
    let ip: string | null | undefined = null;
    try {
      ip = visitorAddress(req, headers, options, warnTrustAll);
    } catch {
      ip = null; // a clientIp that throws: no address (agents are still asked to pay by user agent)
    }
    // Behind a proxy, set "trust proxy" so req.protocol is the visitor's.
    const url = requestUrl(req.protocol, req.get("host"), req.originalUrl);
    siteGate
      .check({ method: req.method, url, headers, ip: ip ?? null })
      .then((decision) => {
        if (decision.action === "respond") {
          try {
            res.status(decision.status).set(decision.headers).send(decision.body);
          } catch (error) {
            // The gate's own answer (a 402, a 503) couldn't be sent: an error, never the page.
            next(error);
          }
          return;
        }
        if (decision.paymentResponse) {
          try {
            res.setHeader("PAYMENT-RESPONSE", decision.paymentResponse);
          } catch {
            // The payment settled: serve the page the agent paid for, even without its receipt header.
          }
        }
        next();
      })
      .catch((error: unknown) => {
        // check() itself never rejects; this is the decision above failing in an unexpected way. Never serve on it.
        next(error);
      });
  };
}

/** Where the visitor's address comes from. A "trust proxy" that trusts every hop (`true`, a function or ranges that
 * trust any address) makes Express's req.ip the left-most X-Forwarded-For entry: the visitor's to write. A spoofed
 * operator address with an agent's user agent would get a free pass or that agent's price, so then the gate takes the
 * last entry, the one the nearest proxy added. */
function visitorAddress(
  req: ExpressRequest,
  headers: { get(name: string): string | null },
  options: ExpressGateOptions,
  warnTrustAll: () => void,
): string | null | undefined {
  if (options.clientIp) return addressOf(options.clientIp(req));
  if (options.ipHeader) return addressFrom(headers, options.ipHeader, null);
  if (req.app && trustsEveryHop(req.app)) {
    warnTrustAll();
    return addressFrom(headers, "x-forwarded-for", null) ?? req.socket?.remoteAddress ?? null;
  }
  return req.ip;
}

const everyHop = new WeakMap<object, boolean>();
// Addresses no proxy has (documentation ranges, far apart): a setting that trusts all of them, at a hop no chain
// reaches, trusts every address. A hop count (`1`, `2`) trusts only that many hops and isn't caught here.
const STRANGERS = [
  ["192.0.2.1", "198.51.100.1", "203.0.113.1"],
  ["2001:db8::1", "3fff:fff::1"],
];

/** Whether Express's "trust proxy" setting trusts every hop: `true`, or its compiled form ("trust proxy fn") trusting
 * any address at any distance (`() => true`, "0.0.0.0/0", ["0.0.0.0/1", "128.0.0.0/1"]). */
function trustsEveryHop(app: { get(setting: string): unknown }): boolean {
  if (app.get("trust proxy") === true) return true;
  const trust = app.get("trust proxy fn");
  if (typeof trust !== "function") return false;
  let answer = everyHop.get(trust);
  if (answer === undefined) {
    try {
      answer = STRANGERS.some((family) => family.every((address) => trust(address, 1_000_000) === true));
    } catch {
      answer = false;
    }
    everyHop.set(trust, answer);
  }
  return answer;
}

export type { GateDecision, GateOptions } from "./core.js";
