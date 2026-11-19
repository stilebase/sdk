// What a gate says to Stilebase and how it reads the answers (SPEC.md). No decisions here: who's an agent, prices and
// payments are Stilebase's; conformance/vectors.json holds the cases every implementation must agree on.

/** The settings a gate runs on: GET /v1/gate/sites/{id}/settings (SPEC.md section 3). */
export interface GateSettings {
  settings_version: number;
  enabled: boolean;
  refresh_seconds: number;
  timeout_ms: number;
  settle_timeout_ms: number;
  breaker: { failures: number; open_seconds: number };
  remember_by: string[];
  cache_max_entries: number;
  strip_headers: string[];
  forward_cookies: string[];
  skip_paths: { prefixes: string[]; extensions: string[] };
  settle_after: { max_bytes: number };
}

/** What a gate needs to know about a request, whatever the framework. */
export interface GateRequest {
  method: string;
  /** The path with the query, as the framework gives it; never the host. */
  path: string;
  /** How the app was reached: "https" or "http" (sent as https while assumeHttps, behind a TLS-terminating proxy). */
  scheme: string;
  /** Every header as received, in order (names in any case). */
  headers: Array<[string, string]>;
  /** The visitor's address, as far as it can be trusted (see the ipHeader option). */
  ip?: string | null;
  /** What the edge knows (Cloudflare's request.cf): "HTTP/1.1", "HTTP/2"…, and the network's AS number. */
  httpProtocol?: string | null;
  asn?: number | null;
}

export const SETTINGS_VERSION = 1;
const ALWAYS_STRIPPED = ["cookie", "authorization", "proxy-authorization"];
const MAX_HEADERS = 100;

const DEFAULTS: GateSettings = {
  settings_version: SETTINGS_VERSION,
  enabled: true,
  refresh_seconds: 300,
  timeout_ms: 300,
  settle_timeout_ms: 65_000,
  breaker: { failures: 5, open_seconds: 30 },
  remember_by: ["client_ip", "user-agent", "path"],
  cache_max_entries: 10_000,
  strip_headers: [],
  forward_cookies: [],
  skip_paths: { prefixes: [], extensions: [] },
  settle_after: { max_bytes: 10_485_760 },
};

/** A settings document from Stilebase (SPEC.md section 3, conformance `settings`): null when it isn't one this version
 * reads; otherwise every field that's missing or malformed takes its default. */
export function parseSettings(value: unknown): GateSettings | null {
  if (!isRecord(value) || value.settings_version !== SETTINGS_VERSION || typeof value.enabled !== "boolean") return null;
  const breaker = isRecord(value.breaker) ? value.breaker : {};
  const skip = isRecord(value.skip_paths) ? value.skip_paths : {};
  const settle = isRecord(value.settle_after) ? value.settle_after : {};
  return {
    settings_version: SETTINGS_VERSION,
    enabled: value.enabled,
    refresh_seconds: positive(value.refresh_seconds, DEFAULTS.refresh_seconds),
    timeout_ms: positive(value.timeout_ms, DEFAULTS.timeout_ms),
    settle_timeout_ms: positive(value.settle_timeout_ms, DEFAULTS.settle_timeout_ms),
    breaker: { failures: positive(breaker.failures, DEFAULTS.breaker.failures), open_seconds: positive(breaker.open_seconds, DEFAULTS.breaker.open_seconds) },
    remember_by: strings(value.remember_by, DEFAULTS.remember_by),
    cache_max_entries: positive(value.cache_max_entries, DEFAULTS.cache_max_entries),
    strip_headers: strings(value.strip_headers, []),
    forward_cookies: strings(value.forward_cookies, []),
    skip_paths: { prefixes: strings(skip.prefixes, []), extensions: strings(skip.extensions, []) },
    settle_after: { max_bytes: positive(settle.max_bytes, DEFAULTS.settle_after.max_bytes) },
  };
}

/** The settings when there are none to read (settling a payment whose settings were dropped meanwhile). */
export function defaultSettings(): GateSettings {
  return structuredClone(DEFAULTS);
}

function positive(value: unknown, fallback: number): number {
  return Number.isInteger(value) && (value as number) > 0 ? (value as number) : fallback;
}

function strings(value: unknown, fallback: string[]): string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string") ? (value as string[]) : fallback;
}

/** Whether a request passes without asking (SPEC.md section 5): matched as the site's router will read it, so a page
 * can't pass for a file. No decoding: any percent-encoding, `;`, `\`, `..` or control character means "ask". */
export function skipped(path: string, skip: GateSettings["skip_paths"]): boolean {
  const bare = path.split("#")[0]!.split("?")[0]!;
  if (/[%;\\]/.test(bare) || bare.includes("..") || [...bare].some((c) => c.charCodeAt(0) < 0x20 || c.charCodeAt(0) === 0x7f)) return false;
  const lower = bare.toLowerCase();
  if (skip.prefixes.some((prefix) => prefix && lower.startsWith(prefix.toLowerCase()))) return true;
  const last = lower.slice(lower.lastIndexOf("/") + 1);
  return skip.extensions.some((ext) => ext && last.endsWith(ext.toLowerCase()) && last.length > ext.length);
}

/** The value a remember key is the SHA-256 of (SPEC.md section 6, step 3): the remember_by fields joined with "\n". */
export function rememberMaterial(request: GateRequest, by: string[]): string {
  return by
    .map((field) => {
      if (field === "client_ip") return normalizeIp(request.ip) ?? "";
      if (field === "path") return request.path;
      return header(request, field) ?? "";
    })
    .join("\n");
}

/** The request's payment header (PAYMENT-SIGNATURE, else the v1 X-PAYMENT); an empty one is none. */
export function paymentOf(request: GateRequest): string | null {
  return header(request, "payment-signature") || header(request, "x-payment") || null;
}

/** The decide body for a request (SPEC.md section 6, step 4; conformance decide_bodies). */
export function decideBody(request: GateRequest, settings: GateSettings, assumeHttps: boolean): Record<string, unknown> {
  // Credentials are never sent, whatever the settings say (strip_headers adds to these, never removes them).
  const strip = new Set([...ALWAYS_STRIPPED, ...settings.strip_headers.map((h) => h.toLowerCase())]);
  const headers: Array<[string, string]> = [];
  let credentials = false;
  for (const [name, value] of request.headers) {
    const lower = name.toLowerCase();
    if (lower === "cookie" || lower === "authorization") credentials = true;
    if (!strip.has(lower) && headers.length < MAX_HEADERS) headers.push([lower, value]);
  }
  const body: Record<string, unknown> = {
    request: { method: request.method.toUpperCase(), scheme: assumeHttps ? "https" : request.scheme.toLowerCase(), path: request.path },
    client_ip: normalizeIp(request.ip),
    headers,
    cookies: forwardedCookies(request, settings.forward_cookies),
    credentials,
  };
  if (request.asn != null || request.httpProtocol != null) {
    body.edge = { asn: request.asn ?? null, http_protocol: request.httpProtocol ?? null };
  }
  return body;
}

function forwardedCookies(request: GateRequest, names: string[]): Record<string, string> {
  const wanted = new Set(names);
  const found: Record<string, string> = {};
  for (const [name, value] of request.headers) {
    if (name.toLowerCase() !== "cookie") continue;
    for (const pair of value.split(";")) {
      const at = pair.indexOf("=");
      if (at < 0) continue;
      const key = pair.slice(0, at).trim();
      if (wanted.has(key) && !(key in found)) found[key] = pair.slice(at + 1).trim();
    }
  }
  return found;
}

/** What Stilebase said, as a gate acts on it (SPEC.md section 6, step 5; conformance answers). */
export type Answer =
  | { kind: "pass"; rememberSeconds: number | null; responseHeaders: Record<string, string>; capture: Capture | null }
  | { kind: "respond"; status: number; headers: Record<string, string>; body: string }
  | { kind: "settle_after"; maxBytes: number; responseHeaders: Record<string, string>; capture: Capture | null }
  | { kind: "unknown_payment"; retryAfter: number }
  | { kind: "unusable" };

export interface Capture {
  token: string;
  maxBytes: number;
}

/** Reads decide's (or settle's) answer (SPEC.md section 6 step 5, conformance `answers`); anything a gate can't act on is
 * "unusable". */
export function readAnswer(status: number, body: unknown): Answer {
  if (status !== 200 || !isRecord(body)) return { kind: "unusable" };
  const capture = readCapture(body.capture);
  switch (body.action) {
    case "pass": {
      const headers = body.response_headers === undefined ? {} : stringRecord(body.response_headers);
      if (headers === null) return { kind: "unusable" };
      if (body.settle_after !== undefined) {
        if (!isRecord(body.settle_after)) return { kind: "unusable" }; // a field of the wrong type
        const max = body.settle_after.max_bytes;
        return Number.isInteger(max) && (max as number) > 0 ? { kind: "settle_after", maxBytes: max as number, responseHeaders: headers, capture } : { kind: "unusable" };
      }
      const seconds = isRecord(body.remember) ? body.remember.seconds : undefined;
      const remember = Number.isInteger(seconds) && (seconds as number) > 0 ? (seconds as number) : null;
      return { kind: "pass", rememberSeconds: remember, responseHeaders: headers, capture };
    }
    case "respond": {
      const headers = body.headers === undefined || body.headers === null ? {} : stringRecord(body.headers);
      const text = body.body === undefined ? "" : body.body;
      const ok = Number.isInteger(body.status) && (body.status as number) >= 200 && (body.status as number) <= 599;
      if (!ok || headers === null || typeof text !== "string") return { kind: "unusable" };
      return { kind: "respond", status: body.status as number, headers, body: text };
    }
    case "unknown_payment": {
      const after = body.retry_after;
      return { kind: "unknown_payment", retryAfter: Number.isInteger(after) && (after as number) >= 1 && (after as number) <= 3600 ? (after as number) : 5 };
    }
    default:
      return { kind: "unusable" };
  }
}

function readCapture(value: unknown): Capture | null {
  if (!isRecord(value) || typeof value.token !== "string" || !Number.isInteger(value.max_bytes) || (value.max_bytes as number) <= 0) return null;
  return { token: value.token, maxBytes: value.max_bytes as number };
}

function stringRecord(value: unknown): Record<string, string> | null {
  if (!isRecord(value)) return null;
  const entries = Object.entries(value);
  return entries.every(([, v]) => typeof v === "string") ? (Object.fromEntries(entries) as Record<string, string>) : null;
}

export function header(request: GateRequest, name: string): string | null {
  const lower = name.toLowerCase();
  const found = request.headers.filter(([n]) => n.toLowerCase() === lower).map(([, v]) => v);
  return found.length ? found.join(", ") : null;
}

/** The visitor's address as the operators' lists hold it: trimmed; ::ffff:1.2.3.4 is 1.2.3.4 (conformance client_ips). */
export function normalizeIp(value: string | null | undefined): string | null {
  const trimmed = (value ?? "").trim();
  if (!trimmed) return null;
  const mapped = /^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/i.exec(trimmed);
  return mapped ? mapped[1]! : trimmed;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
