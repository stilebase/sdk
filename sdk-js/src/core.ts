// The gate, framework-neutral: what Stilebase's Cloudflare Worker does, for any JavaScript runtime. People pass straight
// through; AI agents get 402 Payment Required with the price (x402), and a payment is settled through Stilebase. When
// Stilebase can't be reached, everyone passes: the gate never breaks the site. SPEC.md is the spec.

import {
  type GateConfig,
  type GateRequest,
  type Network,
  fitEncoded,
  inNetworks,
  isAiAgent,
  isScript,
  normalizeIp,
  parseConfig,
  parseNetwork,
  pathOf,
  suspectSignals,
} from "./detect.js";
import { base64, hasPass, parseSiteKey, passToken, signedHeaders } from "./signing.js";
import { PACKAGE, VERSION } from "./version.js";

export const DEFAULT_API_URL = "https://stilebase.com";
const CONFIG_TIMEOUT_MS = 3_000;
const REQUIREMENTS_TIMEOUT_MS = 5_000;
const SETTLE_TIMEOUT_MS = 65_000; // longer than Stilebase takes at most: giving up first would answer 503 for a payment that then settles
const REPORT_TIMEOUT_MS = 10_000;
const RETRY_WITHOUT_CONFIG_MS = 30_000;
const MIN_REFRESH_MS = 30_000;
const RANGES_MAX_AGE_MS = 60 * 60 * 1000;
const MAX_PATH = 2048; // Stilebase's limits for a URL or path, and a user agent
const MAX_USER_AGENT = 512;
const MAX_CACHED = 1000;
const MAX_PENDING_REPORTS = 100;
const WEB_BOT_AUTH_HEADERS = ["signature", "signature-input", "signature-agent"];
const EMPTY = new Uint8Array();

export interface GateOptions {
  /** The site key from your gateway page. Default: the STILEBASE_KEY environment variable. */
  key?: string;
  /** Stilebase's API. Default: STILEBASE_API_URL, else https://stilebase.com. */
  apiUrl?: string;
  /** Scripts with their default user agent (curl, python-requests…) are asked to pay. Default true. */
  catchHttpClients?: boolean;
  /** Agents behind a browser's user agent (the operators' addresses, Web Bot Auth) are asked too. Default true. */
  catchHiddenAgents?: boolean;
  /** Browser-looking requests that don't act like a browser are counted, or checked under strict detection. Default true. */
  checkBrowsers?: boolean;
  /** Where the gate reports what went wrong. Default: a console warning, at most once a minute per message. */
  onError?: (error: Error) => void;
  /** For tests: the fetch to reach Stilebase with, and the clock. */
  fetch?: typeof fetch;
  now?: () => number;
}

export type GateDecision =
  | { action: "pass"; paymentResponse?: string }
  | { action: "respond"; status: number; headers: Record<string, string>; body: string };

/** Lets a runtime keep background work (visit reports, refreshes) running after the response: Workers' ctx.waitUntil. */
export type WaitUntil = (task: Promise<unknown>) => void;

export interface Gate {
  /** What to do with this request. Never throws. */
  check(request: GateRequest, waitUntil?: WaitUntil): Promise<GateDecision>;
}

/** A gate for your site. Adapters (express, fetch) wrap it; use it directly for anything else. */
export function createGate(options: GateOptions = {}, adapter = "core"): Gate {
  return new SiteGate(options, adapter);
}

const pass = (): GateDecision => ({ action: "pass" });

class SiteGate implements Gate {
  private readonly site: { gatewayId: string; secret: string } | null;
  private readonly api: string;
  private readonly client: string;
  private readonly fetch: typeof fetch;
  private readonly now: () => number;
  private config: GateConfig | null = null;
  private configAt = 0;
  private configLoading: Promise<void> | null = null;
  private configLoadingAt = 0;
  private ranges: Network[] | null = null;
  private rangesAt = 0;
  private rangesLoading: Promise<void> | null = null;
  private rangesLoadingAt = 0;
  private readonly cache = new Map<string, { status: number; body: unknown; expires: number }>();
  private readonly warned = new Map<string, number>();
  private pendingReports = 0;

  constructor(
    private readonly options: GateOptions,
    adapter: string,
  ) {
    const key = options.key ?? env("STILEBASE_KEY") ?? "";
    this.site = parseSiteKey(key);
    this.api = (options.apiUrl || env("STILEBASE_API_URL") || DEFAULT_API_URL).replace(/\/+$/, "");
    this.client = `${PACKAGE}/${VERSION} gate/${adapter}`;
    // Never a redirect (signed headers mustn't go elsewhere; a redirect is an answer like any other non-200).
    const given = options.fetch ?? ((input, init) => globalThis.fetch(input, init));
    this.fetch = (input, init) => given(input, { ...init, redirect: "manual" });
    this.now = options.now ?? Date.now;
    if (!this.site) this.report(new Error(key ? "STILEBASE_KEY isn't a site key (sbk_…): every request passes" : "STILEBASE_KEY isn't set: every request passes"));
  }

  async check(request: GateRequest, waitUntil?: WaitUntil): Promise<GateDecision> {
    try {
      return await this.decide(request, waitUntil);
    } catch (error) {
      this.report(error);
      return pass();
    }
  }

  private async decide(request: GateRequest, waitUntil?: WaitUntil): Promise<GateDecision> {
    const site = this.site;
    if (!site) return pass();
    const config = await this.settings(waitUntil);
    if (!config) return pass();
    const path = pathOf(request.url);
    if (config.verification && path.split("?")[0] === config.verification.path) {
      return respond(200, { "content-type": "text/plain; charset=utf-8" }, `${config.verification.value}\n`);
    }
    if (!config.gateway.live) return pass();

    const headers = request.headers;
    const payment = headers.get("payment-signature") || headers.get("x-payment") || null; // an empty header is none
    const userAgent = headers.get("user-agent") ?? "";
    const ip = typeof request.ip === "string" ? normalizeIp(request.ip) : null;
    let signals: string[] = []; // a browser-looking request that failed the header checks: which ones
    if (!payment && !(await this.isAgent(userAgent, headers, ip, config, waitUntil))) {
      signals = this.options.checkBrowsers !== false ? suspectSignals(request, config) : [];
      if (!signals.length) return pass();
      if (await hasPass(headers.get("cookie") ?? "", config.pass.cookie, site.secret, userAgent, this.now(), config.pass.max_age_seconds)) return pass();
    }

    // Cut to Stilebase's limits: an over-long URL or user agent must never make its call fail (that would serve the page).
    const host = config.gateway.domain;
    const siteRequest = { method: request.method.toUpperCase(), path: path.slice(0, MAX_PATH) };
    const agent = {
      ip: ip && ip.length <= 45 ? ip : null,
      user_agent: headers.get("user-agent")?.slice(0, MAX_USER_AGENT) ?? null,
      headers: Object.fromEntries(WEB_BOT_AUTH_HEADERS.flatMap((h) => (headers.get(h) ? [[h, headers.get(h)!]] : []))),
    };
    // The page by the gateway's domain (the Host header is the visitor's to write), encoded well under proxies' 8 KB.
    const page = `https://${host}${path}`.slice(0, MAX_PATH);
    const query = new URLSearchParams({ host, url: fitEncoded(page, 4096), ua: fitEncoded(agent.user_agent ?? "", 1536), ip: agent.ip ?? "" });
    if (signals.length) query.set("suspect", "true");

    let required: Record<string, unknown>;
    try {
      const answer = await this.requirements(query);
      if (answer.status === 204 && !payment) {
        // A free pass: this agent reads for free. Or a suspect while strict detection is off: counted, served.
        this.reportVisit(signals.length ? { host, signals, request: siteRequest, agent } : { host, free_pass: true, request: siteRequest, agent }, waitUntil);
        return pass();
      }
      if (answer.status !== 200 || !isRecord(answer.body)) return pass(); // no live gateway for this domain (yet)
      required = answer.body;
    } catch (error) {
      this.report(error);
      return pass(); // Stilebase unreachable: never break the site
    }

    if (!payment) {
      this.reportVisit(signals.length ? { host, signals, request: siteRequest, agent } : { host, request: siteRequest, agent }, waitUntil);
      if (signals.length && (headers.get("accept") ?? "").includes("text/html")) {
        // The whole user agent, as hasPass checks it (agent.user_agent is cut for Stilebase's limits).
        return browserCheck(required, await passToken(site.secret, userAgent, Math.floor(this.now() / 1000)), config);
      }
      return paymentRequired(required);
    }

    let settlement: Record<string, unknown>;
    try {
      const answer = await this.post("/v1/x402/settle", { host, payment, request: siteRequest, agent }, SETTLE_TIMEOUT_MS);
      if (answer.status !== 200) {
        discard(answer);
        throw new Error(`Stilebase answered the settlement ${answer.status}`);
      }
      const body: unknown = await answer.json();
      if (!isRecord(body) || typeof body.success !== "boolean") throw new Error("Stilebase's settlement answer has no success");
      settlement = body;
    } catch (error) {
      // The outcome is unknown (the payment may have settled): never serve the page, never say nothing was charged.
      this.report(error);
      return respond(503, { "content-type": "application/json", "retry-after": "5" }, JSON.stringify({ error: "Payment settlement unavailable" }));
    }
    const receipt = typeof settlement.header === "string" && /^[A-Za-z0-9+/=_-]{1,8192}$/.test(settlement.header) ? settlement.header : undefined;
    if (!settlement.success) return paymentRequired({ ...required, error: settlement.errorReason ?? "payment_rejected" }, receipt);
    return receipt ? { action: "pass", paymentResponse: receipt } : pass();
  }

  /** An AI agent by its user agent; a script by its default user agent (or none); an agent behind a browser's user
   * agent by Web Bot Auth's header or the address it comes from. */
  private async isAgent(userAgent: string, headers: GateRequest["headers"], ip: string | null, config: GateConfig, waitUntil?: WaitUntil): Promise<boolean> {
    if (isAiAgent(userAgent, config)) return true;
    if (this.options.catchHttpClients !== false && isScript(userAgent, config)) return true;
    if (this.options.catchHiddenAgents === false) return false;
    if (headers.get("signature-agent")) return true;
    if (!ip) return false;
    return inNetworks(ip, await this.agentRanges(waitUntil));
  }

  /** The settings: fetched on the first request, then in the background every refresh_seconds. */
  private async settings(waitUntil?: WaitUntil): Promise<GateConfig | null> {
    // A load past its own timeout never ends (a runtime cancelled the request that started it): start another.
    const fresh = !this.configLoading || this.now() - this.configLoadingAt > CONFIG_TIMEOUT_MS + 500;
    if (!this.configAt) {
      if (fresh) this.background(this.startSettings(), waitUntil); // kept alive by the runtime, and waited on
      await bounded(this.configLoading!, CONFIG_TIMEOUT_MS + 500);
      return this.config;
    }
    const every = this.config ? Math.max(this.config.refresh_seconds * 1000, MIN_REFRESH_MS) : RETRY_WITHOUT_CONFIG_MS;
    if (fresh && this.now() - this.configAt >= every) this.background(this.startSettings(), waitUntil);
    return this.config;
  }

  private startSettings(): Promise<void> {
    this.configLoadingAt = this.now();
    const loading: Promise<void> = this.loadSettings(() => this.configLoading === loading).finally(() => {
      if (this.configLoading === loading) this.configLoading = null;
    });
    return (this.configLoading = loading);
  }

  /** `current()`: false once a newer load replaced this one (it then applies nothing). */
  private async loadSettings(current: () => boolean): Promise<void> {
    const site = this.site!;
    const path = `/v1/x402/sites/${site.gatewayId}/config`;
    try {
      const headers = { ...(await signedHeaders(site.secret, "GET", path, EMPTY, this.now())), "Stilebase-Client": this.client };
      const answer = await this.fetch(this.api + path, { headers, signal: AbortSignal.timeout(CONFIG_TIMEOUT_MS) });
      if (answer.status !== 200) discard(answer);
      if (answer.status === 404) {
        if (current()) this.config = null; // the gateway was removed: stop asking agents to pay
        throw new Error("Stilebase has no gateway for this site key (404): every request passes");
      }
      // Any other failure keeps the last good settings (a 401 after a rotated key: agents are still asked to pay).
      if (answer.status !== 200) throw new Error(`Stilebase answered the settings ${answer.status}`);
      const config = parseConfig(await answer.json());
      if (!config) throw new Error("Stilebase sent settings this version can't read");
      if (current()) this.config = config;
    } catch (error) {
      this.report(error);
    } finally {
      this.configAt = this.now();
    }
  }

  /** The AI operators' published address ranges: fetched on first use, refreshed in the background once an hour old. */
  private async agentRanges(waitUntil?: WaitUntil): Promise<Network[]> {
    const fresh = !this.rangesLoading || this.now() - this.rangesLoadingAt > REQUIREMENTS_TIMEOUT_MS + 500;
    if (this.ranges === null) {
      if (fresh) this.background(this.startRanges(), waitUntil);
      await bounded(this.rangesLoading!, REQUIREMENTS_TIMEOUT_MS + 500);
    } else if (fresh && this.now() - this.rangesAt > RANGES_MAX_AGE_MS) this.background(this.startRanges(), waitUntil);
    return this.ranges ?? [];
  }

  private startRanges(): Promise<void> {
    this.rangesLoadingAt = this.now();
    const loading: Promise<void> = this.loadRanges(() => this.rangesLoading === loading).finally(() => {
      if (this.rangesLoading === loading) this.rangesLoading = null;
    });
    return (this.rangesLoading = loading);
  }

  private async loadRanges(current: () => boolean): Promise<void> {
    try {
      const answer = await this.fetch(`${this.api}/v1/x402/agent-ranges`, { signal: AbortSignal.timeout(REQUIREMENTS_TIMEOUT_MS) });
      if (answer.status !== 200) {
        discard(answer);
        throw new Error(`Stilebase answered the agent ranges ${answer.status}`);
      }
      const body: unknown = await answer.json();
      if (!isRecord(body) || !Array.isArray(body.prefixes)) throw new Error("Stilebase's agent ranges have no prefixes list");
      const prefixes: unknown[] = body.prefixes;
      if (current()) this.ranges = prefixes.flatMap((p) => (typeof p === "string" ? [parseNetwork(p)].filter((n): n is Network => n !== null) : []));
    } catch (error) {
      this.report(error);
      this.ranges ??= []; // keep what we had (or none) and try again in an hour
    } finally {
      this.rangesAt = this.now();
    }
  }

  /** The price lookup's answer, cached as long as Stilebase says (at most 5 minutes), as the Worker's edge cache does. */
  private async requirements(query: URLSearchParams): Promise<{ status: number; body: unknown }> {
    const key = query.toString();
    const now = this.now();
    const cached = this.cache.get(key);
    if (cached && cached.expires > now) return cached;
    const answer = await this.fetch(`${this.api}/v1/x402/requirements?${key}`, { signal: AbortSignal.timeout(REQUIREMENTS_TIMEOUT_MS) });
    const body: unknown = answer.status === 200 ? await answer.json() : (discard(answer), null);
    const maxAge = Math.min(Number(/max-age=(\d+)/.exec(answer.headers.get("cache-control") ?? "")?.[1] ?? 0), 300);
    const result = { status: answer.status, body, expires: now + maxAge * 1000 };
    if (maxAge > 0 && (answer.status === 204 || (answer.status === 200 && isRecord(body)))) {
      this.cache.delete(key);
      if (this.cache.size >= MAX_CACHED) this.cache.delete(this.cache.keys().next().value!);
      this.cache.set(key, result);
    }
    return result;
  }

  /** A signed POST to Stilebase's x402 API. */
  private async post(path: string, payload: unknown, timeoutMs: number): Promise<Response> {
    const body = new TextEncoder().encode(JSON.stringify(payload));
    const headers = { "content-type": "application/json", ...(await signedHeaders(this.site!.secret, "POST", path, body, this.now())) };
    return this.fetch(this.api + path, { method: "POST", body, headers, signal: AbortSignal.timeout(timeoutMs) });
  }

  /** A visit for the owner's statistics, in the background (dropped while too many are in flight: Stilebase slow). */
  private reportVisit(visit: unknown, waitUntil?: WaitUntil): void {
    if (this.pendingReports >= MAX_PENDING_REPORTS) return;
    this.pendingReports += 1;
    const task = this.post("/v1/x402/visits", visit, REPORT_TIMEOUT_MS).then(discard).finally(() => {
      this.pendingReports -= 1;
    });
    this.background(task, waitUntil);
  }

  private background(task: Promise<unknown>, waitUntil?: WaitUntil): void {
    const quiet = task.then(
      () => undefined,
      (error: unknown) => this.report(error),
    );
    if (waitUntil) waitUntil(quiet);
  }

  private report(error: unknown): void {
    const err = error instanceof Error ? error : new Error(String(error));
    try {
      if (this.options.onError) return this.options.onError(err);
      const now = this.now();
      if (now - (this.warned.get(err.message) ?? -Infinity) < 60_000) return;
      if (this.warned.size >= 100) this.warned.clear();
      this.warned.set(err.message, now);
      console.warn(`[@stilebase/sdk] ${err.message}`);
    } catch {
      // a failing error handler mustn't break the site either
    }
  }
}

/** The 402 for an agent: the x402 PaymentRequired as the body and, base64-encoded, in PAYMENT-REQUIRED. */
function paymentRequired(required: Record<string, unknown>, paymentResponse?: string): GateDecision {
  const headers: Record<string, string> = { "content-type": "application/json", "PAYMENT-REQUIRED": base64(JSON.stringify(required)) };
  if (paymentResponse) headers["PAYMENT-RESPONSE"] = paymentResponse;
  return respond(402, headers, JSON.stringify(required));
}

/** The 402 for a suspect that asks for HTML: a page whose script lets a real browser through, the payment request in
 * the header for everyone else. */
function browserCheck(required: Record<string, unknown>, token: string, config: GateConfig): GateDecision {
  const cookie = `${config.pass.cookie}=${token}; Max-Age=${config.pass.max_age_seconds}; Path=/; SameSite=Lax; Secure`;
  const page = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>One moment…</title></head>
<body style="font-family:system-ui,sans-serif;margin:3rem auto;max-width:32rem;padding:0 1rem;color:#334">
<p id="m">Checking your browser…</p>
<noscript><p>This page needs JavaScript. Automated clients: the price is in the PAYMENT-REQUIRED header (x402).</p></noscript>
<script>document.cookie=${JSON.stringify(cookie)};if(document.cookie.indexOf(${JSON.stringify(token)})>=0)location.reload();else document.getElementById("m").textContent="Please allow cookies for this site to continue.";</script>
</body></html>`;
  return respond(402, { "content-type": "text/html; charset=utf-8", "PAYMENT-REQUIRED": base64(JSON.stringify(required)) }, page);
}

/** An answer the gate makes itself, never kept by a cache in front of the site. */
function respond(status: number, headers: Record<string, string>, body: string): GateDecision {
  return { action: "respond", status, headers: { ...headers, "cache-control": "no-store" }, body };
}

/** A clientIp's answer: a string, or no address (a promise's rejection is handled, never left to stop the process). */
export function addressOf(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (value && typeof (value as { then?: unknown }).then === "function") (value as PromiseLike<unknown>).then(undefined, () => undefined);
  return null;
}

/** The visitor's address from a header your proxy sets: from a list (X-Forwarded-For) its last entry, the one your
 * proxy added; the ones before it are the visitor's to write. */
export function addressFrom(headers: GateRequest["headers"], name: string): string | null {
  return headers.get(name)?.split(",").pop()?.trim() || null;
}

/** Releases an answer's body the gate doesn't read (on Node an unread body holds its connection). */
function discard(answer: Response): void {
  answer.body?.cancel().catch(() => undefined);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `promise`, or nothing once `ms` have passed: this request's own deadline, whatever happens to the work it waits on. */
async function bounded(promise: Promise<unknown>, ms: number): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<void>((resolve) => {
    timer = setTimeout(resolve, ms);
  });
  try {
    await Promise.race([promise, deadline]);
  } finally {
    clearTimeout(timer);
  }
}

/** An environment variable where there is one (Node, Bun, Deno with permission); undefined elsewhere (Workers). */
function env(name: string): string | undefined {
  try {
    const value = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env?.[name];
    return typeof value === "string" ? value : undefined;
  } catch {
    return undefined;
  }
}
