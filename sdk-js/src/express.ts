// Express (4 and 5) middleware: `app.use(gate())`, before your routes.

import type { ServerResponse } from "node:http";
import { parse } from "node:url";

import { type Gate, type GateOptions, type SettleDecision, type Ticket, addressFrom, addressOf, createGate } from "./core.js";
import { type Capture, type GateRequest, normalizeIp } from "./protocol.js";

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
    siteGate.check(gateRequest(req, options)).then(
      (decision) => {
        try {
          if (decision.action === "respond") return answer(res, decision);
          if (decision.action === "hold") {
            hold(res, siteGate, decision.ticket, decision.maxBytes, decision.capture);
            return next();
          }
          for (const [name, value] of Object.entries(decision.responseHeaders)) res.setHeader(name, value);
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
  res.statusCode = decision.status;
  for (const [name, value] of Object.entries(decision.headers)) res.setHeader(name, value);
  res.end(decision.body);
}

type Write = ServerResponse["write"];
type End = ServerResponse["end"];

function chunkOf(chunk: unknown, encoding?: unknown): Buffer | null {
  if (chunk == null || typeof chunk === "function") return null;
  if (typeof chunk === "string") return Buffer.from(chunk, typeof encoding === "string" ? (encoding as BufferEncoding) : "utf8");
  if (chunk instanceof Uint8Array) return Buffer.from(chunk.buffer, chunk.byteOffset, chunk.byteLength);
  return null;
}

function headerList(res: ServerResponse): Array<[string, string]> {
  return Object.entries(res.getHeaders()).flatMap(([name, value]) =>
    value === undefined ? [] : Array.isArray(value) ? value.map((v) => [name, String(v)] as [string, string]) : [[name, String(value)] as [string, string]],
  );
}

/** Copies the answer as it goes out (capture): nothing is held back or changed. */
function copy(res: ServerResponse, gate: Gate, capture: Capture): void {
  const started = Date.now();
  let ttfb: number | null = null;
  const parts: Buffer[] = [];
  let kept = 0;
  let size = 0;
  const take = (chunk: unknown, encoding?: unknown) => {
    const bytes = chunkOf(chunk, encoding);
    if (!bytes) return;
    ttfb ??= Date.now() - started;
    size += bytes.byteLength;
    if (kept < capture.maxBytes) {
      const part = bytes.subarray(0, capture.maxBytes - kept);
      parts.push(Buffer.from(part));
      kept += part.byteLength;
    }
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
        body: Buffer.concat(parts),
        size,
        truncated: size > capture.maxBytes,
        ttfbMs: ttfb ?? Date.now() - started,
      }),
    );
    return result;
  }) as End;
}

/** Holds the answer (a payment): nothing reaches the agent until Stilebase settled it, or the answer is refused. */
function hold(res: ServerResponse, gate: Gate, ticket: Ticket, maxBytes: number, capture: Capture | null): void {
  const started = Date.now();
  const writeHead = res.writeHead.bind(res) as (...a: unknown[]) => ServerResponse;
  const write = res.write.bind(res) as (...a: unknown[]) => boolean;
  const end = res.end.bind(res) as (...a: unknown[]) => ServerResponse;
  const parts: Buffer[] = [];
  let size = 0;
  let headArgs: unknown[] | null = null;
  let ended = false;
  const take = (chunk: unknown, encoding?: unknown) => {
    const bytes = chunkOf(chunk, encoding);
    if (!bytes) return;
    size += bytes.byteLength;
    if (size <= maxBytes) parts.push(Buffer.from(bytes));
  };
  res.writeHead = ((...args: unknown[]) => {
    headArgs = args; // sent after settling
    if (typeof args[0] === "number") res.statusCode = args[0];
    return res;
  }) as ServerResponse["writeHead"];
  res.flushHeaders = () => undefined;
  res.write = ((chunk: unknown, ...args: unknown[]) => {
    take(chunk, typeof args[0] === "string" ? args[0] : undefined);
    const callback = args.find((a) => typeof a === "function") as (() => void) | undefined;
    callback?.();
    return true;
  }) as Write;
  res.end = ((chunk?: unknown, ...args: unknown[]) => {
    if (ended) return res;
    ended = true;
    take(chunk, typeof args[0] === "string" ? args[0] : undefined);
    const callback = args.find((a) => typeof a === "function") as (() => void) | undefined;
    const body = Buffer.concat(parts);
    const ttfbMs = Date.now() - started;
    const finish = (decision: SettleDecision) => {
      res.writeHead = writeHead as ServerResponse["writeHead"];
      res.write = write as Write;
      res.end = end as End;
      if (decision.action === "respond") {
        for (const name of res.getHeaderNames()) res.removeHeader(name);
        answer(res, decision);
        return;
      }
      for (const [name, value] of Object.entries(decision.responseHeaders)) res.setHeader(name, value);
      if (headArgs) writeHead(...headArgs);
      if (capture) safely(() => gate.capture(capture, { status: res.statusCode, headers: headerList(res), body: body.subarray(0, capture.maxBytes), size, truncated: size > capture.maxBytes, ttfbMs }));
      end(body, callback);
    };
    const settling = size > maxBytes ? Promise.resolve(gate.tooLarge()) : gate.settle(ticket, { status: res.statusCode, body });
    settling.then(finish, () => finish(gate.tooLarge()));
    return res;
  }) as End;
}

function safely(work: () => void): void {
  try {
    work();
  } catch {
    // copying never gets in the visitor's way
  }
}

export type { GateDecision, GateOptions } from "./core.js";
