// Express (4 and 5) middleware: `app.use(gate())`, before your routes.

import type { ServerResponse } from "node:http";
import { parse } from "node:url";

import { type Gate, type GateDecision, type GateOptions, type SettleDecision, addressFrom, addressOf, createGate, unknownPayment } from "./core.js";
import { type Capture, Copy, type GateRequest, bodyAllowed, normalizeIp, paymentOf } from "./protocol.js";

/** The parts of Express's request the gate uses (no dependency on Express's types). */
export interface ExpressRequest {
  method: string;
  protocol: string;
  originalUrl: string;
  url?: string | undefined;
  ip?: string | undefined;
  rawHeaders?: string[];
  get(name: string): string | undefined;
}

/** Express's response is Node's ServerResponse with helpers; the gate uses Node's side. */
export type ExpressResponse = ServerResponse;

export interface ExpressGateOptions extends GateOptions {
  /** Read the visitor's address from this header (one your proxy or CDN sets, e.g. "cf-connecting-ip"). Default:
   * Express's req.ip, which follows your "trust proxy" setting. Never a header anyone can set. */
  ipHeader?: string;
  /** Or work it out yourself. */
  clientIp?: (req: ExpressRequest) => string | null | undefined;
}

/** Express middleware: Stilebase decides each request; a payment is settled once your route answered. */
export function gate(options: ExpressGateOptions = {}) {
  const siteGate = createGate(options, "express");
  return function stilebaseGate(req: ExpressRequest, res: ExpressResponse, next: (error?: unknown) => void): void {
    const request = gateRequest(req, options);
    const paying = paymentOf(request) !== null;
    siteGate.check(request).then(
      (decision) => {
        if (decision.action === "respond") {
          // The gate's own answer couldn't be sent: as for any failure, the site; a payment, a bare 503, never the page.
          if (!sendOwn(res, decision, paying)) next();
          return;
        }
        if (decision.action === "hold") {
          try {
            hold(res, siteGate, decision);
          } catch {
            sendOwn(res, unknownPayment(5), true); // holding couldn't be set up: never the paid page unsettled
            return;
          }
          return next();
        }
        try {
          if (Object.keys(decision.responseHeaders).length) overlay(res, decision.responseHeaders);
          if (decision.capture) copy(res, siteGate, decision.capture);
        } catch {
          // a header or copy the response refuses: the page is still served
        }
        next();
      },
      (error: unknown) => next(error),
    );
  };
}

/** Sends the gate's own answer. If it can't be: false for a request without a payment (it goes to the site, nothing
 * having been sent); for a payment, a bare 503 is tried and true returned (never the page). */
function sendOwn(res: ServerResponse, decision: { status: number; headers: Record<string, string>; body: string }, paying: boolean): boolean {
  try {
    answer(res, decision);
    return true;
  } catch {
    if (!paying && !res.headersSent) {
      for (const name of res.getHeaderNames()) res.removeHeader(name);
      res.statusCode = 200;
      return false;
    }
    try {
      if (!res.headersSent) {
        for (const name of res.getHeaderNames()) res.removeHeader(name);
        res.statusCode = 503;
      }
      res.end();
    } catch {
      // nothing more can be sent
    }
    return true;
  }
}

/** Stilebase's headers win over the site's, whenever the site sets them: set now and again as the headers go out (their
 * values are ones every server accepts), with same-name headers dropped from the route's own writeHead. */
function overlay(res: ServerResponse, headers: Record<string, string>): void {
  for (const [name, value] of Object.entries(headers)) safely(() => res.setHeader(name, value));
  const writeHead = res.writeHead as (...a: unknown[]) => ServerResponse;
  res.writeHead = ((...args: unknown[]) => {
    for (const [name, value] of Object.entries(headers)) safely(() => res.setHeader(name, value));
    return writeHead.apply(res, withoutNames(args, headers));
  }) as ServerResponse["writeHead"];
}

/** writeHead's arguments without the headers the overlay sets (removed, never rewritten: the rest are the route's). */
function withoutNames(args: unknown[], headers: Record<string, string>): unknown[] {
  const at = args.findIndex((a, i) => i > 0 && a !== null && typeof a === "object");
  if (at < 0) return args;
  const names = new Set(Object.keys(headers).map((n) => n.toLowerCase()));
  const drop = (name: unknown) => names.has(String(name).toLowerCase());
  const given = args[at];
  let kept: unknown;
  if (Array.isArray(given) && given.length && Array.isArray(given[0])) kept = (given as Array<[string, string]>).filter(([n]) => !drop(n));
  else if (Array.isArray(given)) kept = pairs(given as string[]).filter(([n]) => !drop(n)).flat();
  else kept = Object.fromEntries(Object.entries(given as Record<string, unknown>).filter(([n]) => !drop(n)));
  const out = [...args];
  out[at] = kept; // filtered in its own form: a list stays a list, repeated names stay repeated
  return out;
}

function pairs(flat: string[]): Array<[string, string]> {
  const out: Array<[string, string]> = [];
  for (let i = 0; i + 1 < flat.length; i += 2) out.push([flat[i]!, flat[i + 1]!]);
  return out;
}

function gateRequest(req: ExpressRequest, options: ExpressGateOptions): GateRequest {
  let ip: string | null | undefined = null;
  try {
    ip = options.clientIp ? addressOf(options.clientIp(req)) : options.ipHeader ? addressFrom(req.get(options.ipHeader)) : req.ip;
  } catch {
    ip = null; // a clientIp that throws: no address
  }
  const raw = Array.isArray(req.rawHeaders) ? req.rawHeaders : [];
  const headers: Array<[string, string]> = [];
  for (let i = 0; i + 1 < raw.length; i += 2) headers.push([raw[i]!, raw[i + 1]!]);
  return { method: req.method, path: expressPath(req), scheme: req.protocol === "https" ? "https" : "http", headers, ip: normalizeIp(ip ?? null) };
}

/** The visitor's path and query as Express routes by them (originalUrl: before any mount or rewrite, read with Node's
 * url.parse as parseurl does); "/" only when there's nothing to read. */
function expressPath(req: ExpressRequest): string {
  const target = typeof req.originalUrl === "string" ? req.originalUrl : typeof req.url === "string" ? req.url : "/";
  try {
    const { pathname, search } = parse(target);
    const path = pathname ?? "/";
    return `${path.startsWith("/") ? path : `/${path}`}${search ?? ""}`;
  } catch {
    return "/";
  }
}

function answer(res: ServerResponse, decision: { status: number; headers: Record<string, string>; body: string }): void {
  for (const [name, value] of Object.entries(decision.headers)) res.setHeader(name, value); // may throw: status untouched
  res.statusCode = decision.status;
  res.end(bodyAllowed(decision.status) ? decision.body : undefined);
}

type Write = ServerResponse["write"];
type End = ServerResponse["end"];

function chunkOf(chunk: unknown, encoding?: unknown): Buffer | null {
  if (chunk == null || typeof chunk === "function") return null;
  if (typeof chunk === "string") return Buffer.from(chunk, typeof encoding === "string" ? (encoding as BufferEncoding) : "utf8");
  if (chunk instanceof Uint8Array) return Buffer.from(chunk.buffer, chunk.byteOffset, chunk.byteLength);
  return null;
}

/** The headers about to be sent (a capture keeps them without Set-Cookie: core's captureHeaders). */
function headerList(res: ServerResponse): Array<[string, string]> {
  return Object.entries(res.getHeaders()).flatMap(([name, value]) =>
    value === undefined ? [] : Array.isArray(value) ? value.map((v) => [name, String(v)] as [string, string]) : [[name, String(value)] as [string, string]],
  );
}

/** Copies the answer as it goes out (capture): nothing is held back or changed. */
function copy(res: ServerResponse, gate: Gate, capture: Capture): void {
  const started = Date.now();
  let ttfb: number | null = null;
  const copied = new Copy(capture.maxBytes);
  const take = (chunk: unknown, encoding?: unknown) => {
    const bytes = chunkOf(chunk, encoding);
    if (!bytes) return;
    ttfb ??= Date.now() - started;
    copied.add(bytes);
  };
  const write: Write = res.write.bind(res);
  const end: End = res.end.bind(res);
  res.write = ((chunk: unknown, ...args: unknown[]) => {
    safely(() => take(chunk, args[0]));
    return (write as (...a: unknown[]) => boolean)(chunk, ...args);
  }) as Write;
  res.end = ((chunk?: unknown, ...args: unknown[]) => {
    safely(() => take(chunk, args[0]));
    const result = (end as (...a: unknown[]) => ServerResponse)(chunk, ...args);
    safely(() =>
      gate.capture(capture, {
        status: res.statusCode,
        headers: headerList(res),
        body: copied.bytes(),
        size: copied.size,
        truncated: copied.truncated,
        ttfbMs: ttfb ?? Date.now() - started,
      }),
    );
    return result;
  }) as End;
}

/** Holds the answer (a payment): nothing reaches the agent until Stilebase settled it, or the answer is refused. The
 * response's own methods stay in place behind ours, so middleware mounted after the gate (on-headers hooks) still runs. */
function hold(res: ServerResponse, gate: Gate, decision: Extract<GateDecision, { action: "hold" }>): void {
  const { ticket, maxBytes, capture } = decision;
  const started = Date.now();
  const writeHead = res.writeHead as (...a: unknown[]) => ServerResponse;
  const write = res.write as (...a: unknown[]) => boolean;
  const end = res.end as (...a: unknown[]) => ServerResponse;
  const parts: Buffer[] = [];
  let size = 0;
  let headArgs: unknown[] | null = null;
  let released = false; // settled or refused: our methods pass straight through (unless swallowing)
  let done = false; // the route's end, or the gate's own answer, has been taken
  let swallowing = false; // the gate answered before the route finished: the route's further writes go nowhere
  let callback: (() => void) | undefined;

  const finish = (outcome: SettleDecision) => {
    released = true;
    if (outcome.action === "respond") {
      for (const name of res.getHeaderNames()) res.removeHeader(name);
      for (const [name, value] of Object.entries(outcome.headers)) res.setHeader(name, value);
      res.statusCode = outcome.status;
      end.apply(res, [bodyAllowed(outcome.status) ? outcome.body : undefined]);
      return;
    }
    const body = Buffer.concat(parts);
    const allowed = bodyAllowed(res.statusCode);
    // Paid for: whatever Node refuses below, the page still goes out (a refused header is left out). Stilebase's
    // headers win over the route's, its writeHead's included.
    const overlays = { ...decision.responseHeaders, ...outcome.responseHeaders };
    for (const [name, value] of Object.entries(overlays)) safely(() => res.setHeader(name, value));
    if (headArgs) safely(() => res.writeHead(...(withoutNames(headArgs!, overlays) as Parameters<ServerResponse["writeHead"]>)));
    if (capture) safely(() => gate.capture(capture, { status: res.statusCode, headers: headerList(res), body: body.subarray(0, capture.maxBytes), size, truncated: size > capture.maxBytes, ttfbMs: Date.now() - started }));
    end.apply(res, [allowed ? body : undefined, callback]);
  };
  // Whatever happens finishing (a header Node refuses), the request gets an answer and the process keeps running;
  // never the held page unless the settlement said so.
  const finishSafely = (outcome: SettleDecision) => {
    try {
      finish(outcome);
    } catch {
      released = true;
      try {
        if (!res.headersSent) {
          for (const name of res.getHeaderNames()) res.removeHeader(name);
          res.statusCode = 502;
          res.setHeader("cache-control", "no-store");
        }
        end.apply(res, []);
      } catch {
        // nothing more can be sent
      }
    }
  };
  const tooLarge = () => {
    if (done) return;
    done = swallowing = true;
    finishSafely(gate.tooLarge()); // stop now: the rest of the answer is never read
  };
  const take = (chunk: unknown, encoding?: unknown) => {
    const bytes = chunkOf(chunk, encoding);
    if (!bytes || done) return;
    size += bytes.byteLength;
    if (size > maxBytes) return tooLarge();
    parts.push(Buffer.from(bytes));
  };

  res.writeHead = ((...args: unknown[]) => {
    if (released) return writeHead.apply(res, args);
    headArgs = args; // sent after settling
    if (typeof args[0] === "number") res.statusCode = args[0];
    return res;
  }) as ServerResponse["writeHead"];
  const flushHeaders = res.flushHeaders;
  res.flushHeaders = () => (released ? flushHeaders.call(res) : undefined);
  res.write = ((chunk: unknown, ...args: unknown[]) => {
    if (released) return swallowing ? false : write.apply(res, [chunk, ...args]);
    take(chunk, typeof args[0] === "string" ? args[0] : undefined);
    (args.find((a) => typeof a === "function") as (() => void) | undefined)?.();
    return !done;
  }) as ServerResponse["write"];
  res.end = ((chunk?: unknown, ...args: unknown[]) => {
    if (released) return swallowing ? res : end.apply(res, [chunk, ...args]);
    if (done) return res;
    if (typeof chunk === "function") {
      args = [chunk];
      chunk = undefined;
    }
    take(chunk, typeof args[0] === "string" ? args[0] : undefined);
    if (done) return res; // too large: answered already
    done = true;
    callback = args.find((a) => typeof a === "function") as (() => void) | undefined;
    // A protocol switch can't be held or paid for: not settled.
    if (res.statusCode === 101) return finishSafely(gate.tooLarge()), res;
    // A settlement that fails unexpectedly may have charged: never "not charged", never the page.
    gate.settle(ticket, { status: res.statusCode, body: Buffer.concat(parts) }).then(finishSafely, () => finishSafely(unknownPayment(5)));
    return res;
  }) as ServerResponse["end"];
}

function safely(work: () => void): void {
  try {
    work();
  } catch {
    // copying never gets in the visitor's way
  }
}

export type { GateDecision, GateOptions } from "./core.js";
