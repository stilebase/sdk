// The gate: what a site runs in front of its pages so AI agents pay per request with x402 and everyone else passes
// straight through. The same flow as Stilebase's Cloudflare Worker, with its settings fetched from Stilebase instead
// of copied in. SPEC.md, at the root of this repository, is the spec.
//
// It never breaks the site: without a key, without settings, or when Stilebase can't be reached, every request
// passes. Only a payment Stilebase doesn't confirm is refused (503), since serving it would give the page away for a
// payment that may not have been taken.

import {
  type GateConfig,
  type GateRequest,
  type Network,
  NetworkIndex,
  inNetworks,
  isAiAgent,
  fitEncoded,
  isScript,
  normalizeIp,
  pathForMatching,
  siteUrl,
  parseConfig,
  parseNetwork,
  suspectSignals,
} from "./detect.js";
import { base64, hasPass, parseSiteKey, passToken, signedHeaders } from "./signing.js";
import { PACKAGE, VERSION } from "./version.js";

export const DEFAULT_API_URL = "https://stilebase.com";
const CONFIG_TIMEOUT_MS = 3_000;
const REQUIREMENTS_TIMEOUT_MS = 5_000;
// Longer than Stilebase takes at most to settle: giving up first would answer 503 for a payment that then settles.
const SETTLE_TIMEOUT_MS = 65_000;
const REPORT_TIMEOUT_MS = 10_000;
const RETRY_WITHOUT_CONFIG_MS = 30_000; // no settings yet: try again at most this often, never on every request
const MIN_REFRESH_MS = 30_000;
const RANGES_MAX_AGE_MS = 60 * 60 * 1000;
const REQUIREMENTS_CACHE_SIZE = 1000;
const MAX_PENDING_REPORTS = 1000;
const MAX_REQUIREMENTS_AGE_S = 300;
const WEB_BOT_AUTH_HEADERS = ["signature", "signature-input", "signature-agent"];
const MAX_PATH = 2048; // Stilebase's limits for a URL or path, and a user agent
const MAX_USER_AGENT = 512;
// The price lookup's query, encoded, stays well under the 8 KB request line proxies allow.
const MAX_ENCODED_URL = 4096;
const MAX_ENCODED_USER_AGENT = 1536;
// Stilebase refusing the price lookup (malformed, too long, too many): the gate asks with the last price it had. A
// visitor can provoke each of these (a padded URL, many unique URLs), so letting the request through would serve it free.
const REFUSED = new Set([400, 413, 414, 422, 429, 431]);
const EMPTY = new Uint8Array();
const WARN_EVERY_MS = 10 * 60 * 1000;

export interface GateOptions {
  /** The site key from your gateway page (Site key, Reveal secret). Default: the STILEBASE_KEY environment variable. */
  key?: string;
  /** Stilebase's API. Default: STILEBASE_API_URL, else https://stilebase.com. */
  apiUrl?: string;
  /** "test": only Stilebase's test agent (StilebaseCheck) is asked to pay, so you can try it with nobody affected. */
  mode?: "live" | "test";
  /** Only these paths are gated: prefixes ("/listings/") or a test. Both see the path as routers do: percent-decoded,
   * lowercased, dot segments resolved. Default: every path. */
  paths?: string | readonly string[] | ((pathname: string) => boolean);
  /** Scripts with their default user agent (curl, python-requests…) pay too. Default true. */
  catchHttpClients?: boolean;
  /** Agents behind a browser's user agent (from their operators' published addresses, or Web Bot Auth) pay too. Default true. */
  catchHiddenAgents?: boolean;
  /** Browser-looking requests that don't act like a browser are counted, and with Strict detection asked to pay. Default true. */
  checkBrowsers?: boolean;
  /** Visitors reach the site over HTTPS, whatever scheme the app sees (behind a proxy that ends TLS it sees http). The
   * browser checks rely on it: browsers send their tell-tale headers only over HTTPS. Default true; false for a site
   * really served over plain http. */
  assumeHttps?: boolean;
  /** Told about anything that went wrong (the gate let the request through). Default: a console warning, once in a while. */
  onError?: (error: Error) => void;
  /** For tests, or a custom HTTP client. */
  fetch?: typeof fetch;
  /** For tests: the time in milliseconds. */
  now?: () => number;
}

export type GateDecision =
  /** Serve the request. After a settled payment, add PAYMENT-RESPONSE with this value to your response. */
  | { action: "pass"; paymentResponse?: string }
  /** Answer with this instead (the 402, the browser check, the verification file, or 503). */
  | { action: "respond"; status: number; headers: Record<string, string>; body: string };

/** Keeps a background task alive after the response (Workers' ctx.waitUntil, Next's event.waitUntil). */
export type WaitUntil = (task: Promise<unknown>) => void;

export interface Gate {
  /** What to do with this request. Never throws. */
  check(request: GateRequest, waitUntil?: WaitUntil): Promise<GateDecision>;
  /** What this gate tells Stilebase it is (shown on the gateway page): "@stilebase/sdk/0.1.0 gate/express". */
  readonly client: string;
}

interface Cached {
  status: number;
  body: unknown;
  expires: number;
}

const PASS: GateDecision = { action: "pass" };

/** A gate for your site. Adapters (express, next, fetch) wrap it; use it directly for anything else. */
export function createGate(options: GateOptions = {}, adapter = "core"): Gate {
  return new SiteGate(options, adapter);
}

class SiteGate implements Gate {
  readonly client: string;
  private readonly site: { gatewayId: string; secret: string } | null;
  private readonly api: string;
  private readonly fetch: typeof fetch;
  private readonly now: () => number;
  private readonly testMode: boolean;
  private config: GateConfig | null = null;
  private configCheckedAt = 0;
  private configTried = false;
  private configLoading: Promise<void> | null = null;
  private ranges: NetworkIndex | null = null;
  private rangesAt = 0;
  private rangesLoading: Promise<void> | null = null;
  private readonly requirementsCache = new Map<string, Cached>();
  private readonly warned = new Map<string, number>();
  private pendingReports = 0;
  private lastRequired: Record<string, unknown> | null = null; // the last price Stilebase gave, for refused lookups

  constructor(
    private readonly options: GateOptions,
    adapter: string,
  ) {
    const env = environment();
    this.client = `${PACKAGE}/${VERSION} gate/${adapter}`; // e.g. "@stilebase/sdk/0.1.0 gate/express"
    this.api = (options.apiUrl || env.STILEBASE_API_URL || DEFAULT_API_URL).replace(/\/+$/, ""); // empty counts as unset
    // Called as plain functions, never as this.fetch(...): a fetch with `this` bound to the gate throws on Workers.
    const given = options.fetch;
    this.fetch = given ? (input, init) => given(input, init) : (input, init) => globalThis.fetch(input, init);
    this.now = options.now ?? Date.now;
    this.testMode = options.mode === "test";
    const key = options.key ?? env.STILEBASE_KEY ?? "";
    this.site = parseSiteKey(key);
    if (!this.site) {
      this.report(new Error(key ? "STILEBASE_KEY isn't a site key (sbk_…): every request passes" : "STILEBASE_KEY isn't set: every request passes"));
    }
  }

  async check(request: GateRequest, waitUntil?: WaitUntil): Promise<GateDecision> {
    try {
      return await this.decide(request, waitUntil);
    } catch (error) {
      this.report(error);
      return PASS;
    }
  }

  private async decide(original: GateRequest, waitUntil?: WaitUntil): Promise<GateDecision> {
    const site = this.site;
    if (!site) return PASS;
    // Each field by name: a Fetch Request's are getters, which spreading would drop.
    const request: GateRequest = {
      method: original.method,
      url: original.url,
      headers: original.headers,
      ip: usableIp(normalizeIp(original.ip)),
      httpProtocol: original.httpProtocol ?? null,
      asn: original.asn ?? null,
    };
    const config = await this.settings(waitUntil);
    if (!config) return PASS;
    const url = siteUrl(request.url, config.gateway.domain);
    if (config.verification && url.pathname === config.verification.path) {
      return respond(200, { "content-type": "text/plain; charset=utf-8" }, `${config.verification.value}\n`);
    }
    if (!config.gateway.live || !this.gated(url.pathname)) return PASS;

    const headers = request.headers;
    const payment = headers.get("payment-signature") ?? headers.get("x-payment") ?? null;
    const userAgent = headers.get("user-agent") ?? "";
    let signals: string[] = []; // a browser-looking request that failed the header checks: which ones
    if (!payment && !(await this.isAgent(request, userAgent, config, waitUntil))) {
      const check = this.options.checkBrowsers !== false && !this.testMode;
      signals = check ? suspectSignals(request, config, this.options.assumeHttps !== false) : [];
      if (!signals.length) return PASS;
      const now = this.now();
      if (await hasPass(headers.get("cookie") ?? "", config.pass.cookie, site.secret, userAgent, now, config.pass.max_age_seconds)) return PASS;
    }

    // Cut to Stilebase's limits: an over-long URL or user agent must never make its call fail (that would serve the page).
    const siteRequest = { method: request.method.toUpperCase(), path: (url.pathname + url.search).slice(0, MAX_PATH) };
    const agent = {
      ip: request.ip ?? null,
      user_agent: headers.get("user-agent")?.slice(0, MAX_USER_AGENT) ?? null,
      headers: Object.fromEntries(WEB_BOT_AUTH_HEADERS.flatMap((h) => (headers.get(h) ? [[h, headers.get(h)!]] : []))),
    };
    const host = config.gateway.domain;
    const query = new URLSearchParams({
      host,
      url: fitEncoded(url.href.slice(0, MAX_PATH), MAX_ENCODED_URL),
      ua: fitEncoded(agent.user_agent ?? "", MAX_ENCODED_USER_AGENT),
      ip: agent.ip ?? "",
    });
    if (signals.length) query.set("suspect", "true");
    let required: Record<string, unknown>;
    try {
      const answer = await this.requirements(query);
      if (answer.status === 204 && !payment) {
        // A free pass: this agent reads for free. Or a suspect while strict detection is off: counted, served.
        const visit = signals.length ? { host, signals, request: siteRequest, agent } : { host, free_pass: true, request: siteRequest, agent };
        this.reportVisit(visit, waitUntil);
        return PASS;
      }
      if (answer.status === 200 && isRecord(answer.body)) {
        required = answer.body;
        this.lastRequired = required;
      } else if (REFUSED.has(answer.status) && this.lastRequired) {
        // Stilebase refused the lookup (a request built to be refused?): ask with the last price rather than give the page.
        required = { ...this.lastRequired, resource: { ...(this.lastRequired.resource as object), url: url.href } };
      } else {
        return PASS; // no live gateway for this domain (yet), or no price known
      }
    } catch (error) {
      this.report(error);
      return PASS; // Stilebase unreachable: never break the site
    }

    if (!payment) {
      const visit = signals.length ? { host, signals, request: siteRequest, agent } : { host, request: siteRequest, agent };
      this.reportVisit(visit, waitUntil); // statistics only
      if (signals.length && (headers.get("accept") ?? "").includes("text/html")) {
        return browserCheck(required, await passToken(site.secret, userAgent, Math.floor(this.now() / 1000)), config);
      }
      return paymentRequired(required);
    }

    let settlement: { success?: boolean; errorReason?: string | null; header?: string };
    try {
      const answer = await this.post("/v1/x402/settle", { host, payment, request: siteRequest, agent }, SETTLE_TIMEOUT_MS);
      if (answer.status !== 200) {
        discard(answer);
        throw new Error(`Stilebase answered the settlement ${answer.status}`);
      }
      const body: unknown = await answer.json();
      if (!isRecord(body) || typeof body.success !== "boolean") throw new Error("Stilebase's settlement answer has no boolean success");
      settlement = body as typeof settlement;
    } catch (error) {
      this.report(error);
      // The outcome is unknown: the payment may have settled (an answer lost on the way). Never serve the page, and never
      // tell the agent it wasn't charged. Sending the same payment again can't charge twice.
      return respond(503, { "content-type": "application/json", "retry-after": "5", "cache-control": "no-store" }, JSON.stringify({ error: "Payment settlement unavailable" }));
    }
    if (!settlement.success) {
      return paymentRequired({ ...required, error: settlement.errorReason ?? "payment_rejected" }, settlement.header);
    }
    return settlement.header ? { action: "pass", paymentResponse: settlement.header } : PASS;
  }

  private gated(pathname: string): boolean {
    const paths = this.options.paths;
    if (paths === undefined) return true;
    const path = pathForMatching(pathname); // decoded, lowercased, dot segments resolved: as routers see it
    if (typeof paths === "function") return paths(path);
    return (typeof paths === "string" ? [paths] : paths).some((prefix) => path.startsWith(pathForMatching(prefix)));
  }

  /** An AI agent by its user agent; a script by its default user agent (or none); an agent hiding behind a browser's
   * user agent by Web Bot Auth's header or the address it comes from. In test mode only Stilebase's test agent. */
  private async isAgent(request: GateRequest, userAgent: string, config: GateConfig, waitUntil?: WaitUntil): Promise<boolean> {
    if (isAiAgent(userAgent, config, this.testMode)) return true;
    if (this.testMode) return false;
    if (this.options.catchHttpClients !== false && isScript(userAgent, config)) return true;
    if (this.options.catchHiddenAgents === false) return false;
    if (request.headers.get("signature-agent")) return true;
    if (!request.ip) return false;
    return inNetworks(request.ip, await this.agentRanges(waitUntil));
  }

  /** The settings: fetched on the first request, then refreshed in the background every refresh_seconds. */
  private async settings(waitUntil?: WaitUntil): Promise<GateConfig | null> {
    if (!this.configTried) {
      await this.loadSettings();
      return this.config;
    }
    const every = this.config ? Math.max(this.config.refresh_seconds * 1000, MIN_REFRESH_MS) : RETRY_WITHOUT_CONFIG_MS;
    if (this.now() - this.configCheckedAt >= every) this.background(this.loadSettings(), waitUntil);
    return this.config;
  }

  private loadSettings(): Promise<void> {
    this.configLoading ??= this.fetchSettings().finally(() => {
      this.configTried = true;
      this.configCheckedAt = this.now();
      this.configLoading = null;
    });
    return this.configLoading;
  }

  private async fetchSettings(): Promise<void> {
    const site = this.site!;
    const path = `/v1/x402/sites/${site.gatewayId}/config`;
    try {
      const headers = { ...(await signedHeaders(site.secret, "GET", path, EMPTY, this.now())), "Stilebase-Client": this.client };
      const answer = await this.fetch(this.api + path, { headers, signal: AbortSignal.timeout(CONFIG_TIMEOUT_MS) });
      if (answer.status !== 200) discard(answer);
      if (answer.status === 404) {
        this.config = null; // the gateway was removed: stop asking agents to pay
        throw new Error("Stilebase has no gateway for this site key (404): every request passes");
      }
      if (answer.status === 401) {
        // A rotated key, or this server's clock more than 5 minutes off. Keep the last settings: agents still get the
        // 402 (the price lookup isn't signed) and only payments fail, as with the Worker. Dropping them would let every
        // agent read free until someone notices.
        throw new Error(
          "Stilebase refused the site key (401): was it rotated, or is this server's clock off? Update STILEBASE_KEY. " +
            "Agents are still asked to pay, but their payments can't be settled",
        );
      }
      if (answer.status !== 200) throw new Error(`Stilebase answered the settings ${answer.status}`);
      const config = parseConfig(await answer.json());
      if (!config) throw new Error("Stilebase sent settings this version can't read");
      this.config = config;
    } catch (error) {
      this.report(error); // keep the last good settings, if any
    }
  }

  /** The AI operators' published address ranges: fetched on first use, refreshed in the background once an hour old. */
  private async agentRanges(waitUntil?: WaitUntil): Promise<NetworkIndex> {
    const load = () =>
      (this.rangesLoading ??= (async () => {
        try {
          const answer = await this.fetch(`${this.api}/v1/x402/agent-ranges`, { signal: AbortSignal.timeout(REQUIREMENTS_TIMEOUT_MS) });
          if (answer.status !== 200) {
            discard(answer);
            throw new Error(`Stilebase answered the agent ranges ${answer.status}`);
          }
          const body = (await answer.json()) as { prefixes?: unknown };
          const prefixes = Array.isArray(body.prefixes) ? body.prefixes : [];
          this.ranges = new NetworkIndex(prefixes.flatMap((p) => (typeof p === "string" ? [parseNetwork(p)].filter((n): n is Network => n !== null) : [])));
        } catch (error) {
          this.report(error);
          this.ranges ??= new NetworkIndex([]); // keep what we had (or none) and try again in an hour
        } finally {
          this.rangesAt = this.now();
          this.rangesLoading = null;
        }
      })());
    if (this.ranges === null) await load();
    else if (this.now() - this.rangesAt > RANGES_MAX_AGE_MS) this.background(load(), waitUntil);
    return this.ranges ?? new NetworkIndex([]);
  }

  /** The 402's requirement (200) or a free pass (204), cached as long as Stilebase says (5 minutes). */
  private async requirements(query: URLSearchParams): Promise<{ status: number; body: unknown }> {
    const key = query.toString();
    const now = this.now();
    const cached = this.requirementsCache.get(key);
    if (cached && cached.expires > now) return cached;
    if (cached) this.requirementsCache.delete(key);
    const answer = await this.fetch(`${this.api}/v1/x402/requirements?${key}`, { signal: AbortSignal.timeout(REQUIREMENTS_TIMEOUT_MS) });
    const body = answer.status === 200 ? await answer.json() : (discard(answer), null);
    const maxAge = Math.min(Number(/max-age=(\d+)/.exec(answer.headers.get("cache-control") ?? "")?.[1] ?? 0), MAX_REQUIREMENTS_AGE_S);
    const result = { status: answer.status, body, expires: now + maxAge * 1000 };
    if ((answer.status === 200 || answer.status === 204) && maxAge > 0) {
      if (this.requirementsCache.size >= REQUIREMENTS_CACHE_SIZE) {
        this.requirementsCache.delete(this.requirementsCache.keys().next().value!);
      }
      this.requirementsCache.set(key, result);
    }
    return result;
  }

  /** A signed POST to Stilebase's x402 API. */
  private async post(path: string, payload: unknown, timeoutMs: number): Promise<Response> {
    const body = new TextEncoder().encode(JSON.stringify(payload));
    const headers = { "content-type": "application/json", ...(await signedHeaders(this.site!.secret, "POST", path, body, this.now())) };
    return this.fetch(this.api + path, { method: "POST", body, headers, signal: AbortSignal.timeout(timeoutMs) });
  }

  /** A visit for the owner's statistics, in the background; dropped when MAX_PENDING_REPORTS are already in flight
   * (Stilebase slow): statistics can wait, memory can't grow without end. */
  private reportVisit(visit: unknown, waitUntil?: WaitUntil): void {
    if (this.pendingReports >= MAX_PENDING_REPORTS) return;
    this.pendingReports += 1;
    this.background(
      this.post("/v1/x402/visits", visit, REPORT_TIMEOUT_MS).then(discard).finally(() => {
        this.pendingReports -= 1;
      }),
      waitUntil,
    );
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
    if (this.options.onError) {
      try {
        this.options.onError(err);
      } catch {
        // a failing error handler mustn't break the site either
      }
      return;
    }
    const now = this.now();
    const last = this.warned.get(err.message);
    if (last !== undefined && now - last < WARN_EVERY_MS) return;
    this.warned.set(err.message, now);
    console.warn(`[${PACKAGE}] ${err.message}`);
  }
}

function environment(): Record<string, string | undefined> {
  return (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {};
}

/** No address is longer than 45 characters: anything longer (a header someone stuffed) counts as none, as Stilebase
 * treats it, and never bloats the price lookup past proxies' limits. */
function usableIp(ip: string | null): string | null {
  return ip && ip.length <= 45 ? ip : null;
}

/** Lets go of an answer's body that won't be read, so its connection goes back to the pool (Node's fetch keeps it
 * otherwise until garbage collection). */
function discard(answer: Response): void {
  answer.body?.cancel().catch(() => undefined);
}

function respond(status: number, headers: Record<string, string>, body: string): GateDecision {
  return { action: "respond", status, headers, body };
}

function paymentRequired(required: Record<string, unknown>, paymentResponse?: string): GateDecision {
  const headers: Record<string, string> = {
    "content-type": "application/json",
    "cache-control": "no-store",
    "PAYMENT-REQUIRED": base64(JSON.stringify(required)),
  };
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
  return respond(402, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "PAYMENT-REQUIRED": base64(JSON.stringify(required)) }, page);
}

/** The visitor's address from a header your proxy sets (`ipHeader`), else the adapter's default. A list (as in
 * X-Forwarded-For) gives its last entry: the one your proxy added. The ones before it are the visitor's to write. */
export function addressFrom(headers: GateRequest["headers"], ipHeader: string | undefined, fallback: string | null | undefined): string | null {
  if (ipHeader) return headers.get(ipHeader)?.split(",").pop()?.trim() || null;
  return fallback || null;
}

/** A JSON object (not null, an array or a primitive): the only shape Stilebase's answers are read from. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
