// The gate, framework-neutral (SPEC.md). It has no logic of its own: it forwards each request to Stilebase, does what
// the answer says, and lets the request through when anything goes wrong. A payment is settled only after the site
// answered (the adapter holds the answer); `capture` copies an answer for AgentView.

import {
  type Answer,
  type Capture,
  type GateRequest,
  type GateSettings,
  decideBody,
  defaultSettings,
  notPayableAnswer,
  respondAnswer,
  unknownPaymentAnswer,
  isRecord,
  parseSettings,
  paymentOf,
  readAnswer,
  captureHeaders,
  rememberMaterial,
  skipped,
} from "./protocol.js";
import { base64Bytes, parseSiteKey, sha256Hex, signedHeaders } from "./signing.js";
import { PACKAGE, VERSION } from "./version.js";

export const DEFAULT_API_URL = "https://stilebase.com";
const SETTINGS_TIMEOUT_MS = 3_000;
const RETRY_WITHOUT_SETTINGS_MS = 30_000;
const MIN_REFRESH_MS = 30_000;
const CAPTURE_TIMEOUT_MS = 10_000;
const MAX_CAPTURES_WAITING = 20;
const UNKNOWN_RETRY_SECONDS = 5;
const EMPTY = new Uint8Array();
const MAX_ASKING = 1000;
// A decision cut off by its budget isn't aborted: it runs on, at most this long, so the connection it was opening is
// there for the next request (aborting it would make every call after an idle spell start, and lose, a new handshake).
const KEEP_CONNECTING_MS = 3_000; // a TLS handshake from anywhere fits; few sockets held through an outage
const started = Date.now() - (globalThis.performance?.now() ?? 0);
/** Milliseconds on a clock that never goes back (performance.now), shaped like Date.now. */
const monotonic = () => (globalThis.performance ? started + globalThis.performance.now() : Date.now());

export interface GateOptions {
  /** The site key from your gateway page. Default: the STILEBASE_KEY environment variable. */
  key?: string;
  /** Stilebase's API. Default: STILEBASE_API_URL, else https://stilebase.com. */
  apiUrl?: string;
  /** Every request passes and nothing is asked. Default: STILEBASE_DISABLED=1. */
  disabled?: boolean;
  /** Send the scheme as https whatever the app sees (behind a TLS-terminating proxy). Default true. */
  assumeHttps?: boolean;
  /** Where the gate reports what went wrong. Default: a console warning, at most once a minute per message. */
  onError?: (error: Error) => void;
  /** For tests: the fetch to reach Stilebase with, and the clock (both for intervals and for signing). */
  fetch?: typeof fetch;
  now?: () => number;
}

/** What the adapter does with a request. */
export type GateDecision =
  | { action: "pass"; responseHeaders: Record<string, string>; capture: Capture | null }
  | { action: "respond"; status: number; headers: Record<string, string>; body: string }
  /** A verified payment: hold the site's answer (at most maxBytes of body), then call `settle` with it. The settled
   * answer gets responseHeaders and settle's own. */
  | { action: "hold"; maxBytes: number; responseHeaders: Record<string, string>; capture: Capture | null; ticket: Ticket };

/** An answer the gate makes itself. */
export interface Respond {
  action: "respond";
  status: number;
  headers: Record<string, string>;
  body: string;
}

/** What `settle` says to do with the held answer. */
export type SettleDecision = { action: "send"; responseHeaders: Record<string, string> } | Respond;

/** A held decision's request, as `settle` needs it again. */
export interface Ticket {
  readonly body: Record<string, unknown>;
}

/** The copy `capture` sends: what the site answered, as it went out. */
export interface CapturedAnswer {
  status: number;
  headers: Array<[string, string]>;
  body: Uint8Array; // at most the capture's maxBytes
  size: number; // the whole body's bytes
  truncated: boolean;
  ttfbMs: number;
}

/** Lets a runtime keep background work running after the response: Workers' ctx.waitUntil. */
export type WaitUntil = (task: Promise<unknown>) => void;

export interface Gate {
  /** What to do with this request. Never throws. */
  check(request: GateRequest, waitUntil?: WaitUntil): Promise<GateDecision>;
  /** Settle a held payment now that the site answered. Never throws. */
  settle(ticket: Ticket, delivery: { status: number; body: Uint8Array }): Promise<SettleDecision>;
  /** The answer when a paid body is larger than the gate holds: 502, nothing settled. */
  tooLarge(): Respond;
  /** Send a captured answer in the background (one at a time; dropped when too many wait or it fails). */
  capture(capture: Capture, answer: CapturedAnswer, waitUntil?: WaitUntil): void;
}

/** A gate for your site. Adapters (express, fetch) wrap it; use it directly for anything else. */
export function createGate(options: GateOptions = {}, adapter = "core"): Gate {
  return new SiteGate(options, adapter);
}

const PASS: GateDecision = { action: "pass", responseHeaders: {}, capture: null };

class SiteGate implements Gate {
  private readonly site: { gatewayId: string; secret: string } | null;
  private readonly api: string;
  private readonly client: string;
  private readonly fetch: typeof fetch;
  private readonly now: () => number; // intervals: a clock that never goes back
  private readonly wall: () => number; // signing: the time of day
  private readonly disabled: boolean;
  private readonly assumeHttps: boolean;
  private settings: GateSettings | null = null;
  private settingsAt = 0;
  private settingsLoading: Promise<void> | null = null;
  private settingsLoadingAt = 0;
  private settingsStarted = 0; // generations: the latest load started, and the newest whose answer applied
  private settingsApplied = 0;
  private failures = 0; // the breaker: decisions failed in a row, when it's open until, and a half-open probe out
  private openUntil = 0;
  private probeUntil = 0; // the half-open probe out, and until when it counts as alive (0: none)
  private readonly remembered = new Map<string, number>(); // remember key -> expires (ms), oldest first
  private readonly asking = new Map<string, { answer: Promise<Answer>; since: number }>(); // decisions in flight
  private readonly warned = new Map<string, number>();
  private captureQueue: Array<() => Promise<void>> = [];
  private drain: { active: number } | null = null; // the capture drain running, and when it last made progress

  constructor(
    private readonly options: GateOptions,
    adapter: string,
  ) {
    const key = options.key ?? env("STILEBASE_KEY") ?? "";
    this.site = parseSiteKey(key);
    this.api = (options.apiUrl || env("STILEBASE_API_URL") || DEFAULT_API_URL).replace(/\/+$/, "");
    this.client = `${PACKAGE}/${VERSION} gate/${adapter}`;
    this.disabled = options.disabled ?? switchedOff(env("STILEBASE_DISABLED"));
    this.assumeHttps = options.assumeHttps !== false;
    // Never a redirect (signed headers mustn't go elsewhere; a redirect is an answer like any other non-200).
    const given = options.fetch ?? ((input, init) => globalThis.fetch(input, init));
    this.fetch = (input, init) => given(input, { ...init, redirect: "manual" });
    this.now = options.now ?? monotonic;
    this.wall = options.now ?? Date.now;
    if (!this.site && !this.disabled) {
      this.report(new Error(key ? "STILEBASE_KEY isn't a site key (sbk_…): every request passes" : "STILEBASE_KEY isn't set: every request passes"));
    }
  }

  async check(request: GateRequest, waitUntil?: WaitUntil): Promise<GateDecision> {
    const payment = safe(() => paymentOf(request), null);
    try {
      return await this.decide(request, payment !== null, waitUntil);
    } catch (error) {
      this.report(error);
      return payment !== null ? unknownPayment(UNKNOWN_RETRY_SECONDS) : PASS;
    }
  }

  private async decide(request: GateRequest, paying: boolean, waitUntil?: WaitUntil): Promise<GateDecision> {
    if (this.disabled || !this.site) return PASS;
    const settings = await this.loadedSettings(waitUntil);
    if (!settings || !settings.enabled) return PASS;
    if (!paying && skipped(request.path, settings.skip_paths)) return PASS;
    // The breaker: open, everyone passes (payments 503); half open, one request asks and the rest wait it out.
    // A probe not finished within its budget (plus a second) was cancelled by its runtime: the next request probes.
    let probe = 0;
    if (this.openUntil) {
      if (this.now() < this.openUntil || this.now() < this.probeUntil) return paying ? unknownPayment(UNKNOWN_RETRY_SECONDS) : PASS;
      probe = this.probeUntil = this.now() + (paying ? settings.settle_timeout_ms : settings.timeout_ms) + 1000;
    }
    try {
      return await this.decided(request, settings, paying, probe, waitUntil);
    } finally {
      if (probe && this.probeUntil === probe) this.probeUntil = 0;
    }
  }

  /** `probe`: this request is the half-open probe (its deadline), 0 if not; a stale probe's outcome isn't counted. */
  private async decided(
    request: GateRequest,
    settings: GateSettings,
    paying: boolean,
    probe: number,
    waitUntil?: WaitUntil,
  ): Promise<GateDecision> {
    const site = this.site!;

    const key = await sha256Hex(rememberMaterial(request, settings.remember_by));
    // Requests alike share one decision while it's in flight: GETs and HEADs only, keyed with the method too.
    const method = request.method.toUpperCase();
    const shared = !paying && (method === "GET" || method === "HEAD") ? `${method} ${key}` : null;
    if (!paying) {
      const expires = this.remembered.get(key);
      if (expires !== undefined && expires > this.now()) return PASS;
      if (expires !== undefined) this.remembered.delete(key);
    }

    const budget = paying ? settings.settle_timeout_ms : settings.timeout_ms;
    let answer: Answer;
    let body: Record<string, unknown> | null = null;
    // A shared decision waiting longer than its budget is forgotten: the runtime may have cancelled it.
    let inFlight = shared && probe === 0 ? this.asking.get(shared) : undefined;
    if (inFlight && this.now() - inFlight.since > budget) {
      this.asking.delete(shared!);
      inFlight = undefined;
    }
    if (inFlight) {
      answer = await withinBudget(inFlight.answer, budget);
      if (answer.kind === "pass") answer = { ...answer, capture: null, rememberSeconds: null }; // only the asker remembers or captures
    } else {
      body = decideBody(request, settings, this.assumeHttps);
      const asked = this.ask(`/v1/gate/sites/${site.gatewayId}/decide`, body, Math.max(budget, KEEP_CONNECTING_MS));
      if (shared) {
        // Entries a runtime left behind (cancelled leaders) are swept once there are many.
        if (this.asking.size > MAX_ASKING) for (const [k, e] of this.asking) if (this.now() - e.since > budget) this.asking.delete(k);
        const entry = { answer: asked, since: this.now() };
        this.asking.set(shared, entry);
        void asked.finally(() => this.asking.get(shared) === entry && this.asking.delete(shared));
      }
      const late = { kind: "unusable" } as const;
      answer = await withinBudget(asked, budget, late);
      if (answer === late) {
        // Past the budget: the request goes on without Stilebase; the call finishes in the background.
        this.report(new Error(`Stilebase didn't answer within ${budget} ms`));
        if (waitUntil) waitUntil(asked);
      }
      // A probe forgotten as stale (another has started since) counts nothing.
      if (probe === 0 || this.probeUntil === probe) this.breaker(answer.kind !== "unusable", settings);
    }

    switch (answer.kind) {
      case "pass": {
        // Only a plain pass is remembered: one with headers or a capture is asked again.
        const plain = !answer.capture && Object.keys(answer.responseHeaders).length === 0;
        if (answer.rememberSeconds && !paying && plain) this.remember(key, answer.rememberSeconds, settings.cache_max_entries);
        return { action: "pass", responseHeaders: answer.responseHeaders, capture: answer.capture };
      }
      case "respond":
        return respond(answer.status, answer.headers, answer.body);
      case "settle_after": {
        const maxBytes = Math.min(answer.maxBytes, settings.settle_after.max_bytes);
        return { action: "hold", maxBytes, responseHeaders: answer.responseHeaders, capture: answer.capture, ticket: { body: body ?? decideBody(request, settings, this.assumeHttps) } };
      }
      case "unknown_payment":
        return unknownPayment(answer.retryAfter);
      default:
        return paying ? unknownPayment(UNKNOWN_RETRY_SECONDS) : PASS;
    }
  }

  async settle(ticket: Ticket, delivery: { status: number; body: Uint8Array }): Promise<SettleDecision> {
    try {
      if (!this.site) return unknownPayment(UNKNOWN_RETRY_SECONDS);
      const settings = this.settings ?? defaultSettings(); // dropped meanwhile: the payment is still settled
      const payload = { ...ticket.body, delivery: { status: delivery.status, size: delivery.body.byteLength, sha256: await sha256Hex(delivery.body) } };
      const answer = await this.ask(`/v1/gate/sites/${this.site.gatewayId}/settle`, payload, settings.settle_timeout_ms);
      // A settle_after in a settle answer is ignored: it's a pass.
      if (answer.kind === "pass" || answer.kind === "settle_after") return { action: "send", responseHeaders: answer.responseHeaders };
      if (answer.kind === "respond") return respond(answer.status, answer.headers, answer.body);
      return unknownPayment(answer.kind === "unknown_payment" ? answer.retryAfter : UNKNOWN_RETRY_SECONDS);
    } catch (error) {
      this.report(error);
      return unknownPayment(UNKNOWN_RETRY_SECONDS);
    }
  }

  tooLarge(): Respond {
    return { action: "respond", ...notPayableAnswer() };
  }

  capture(capture: Capture, answer: CapturedAnswer, waitUntil?: WaitUntil): void {
    try {
      if (!this.site || this.captureQueue.length >= MAX_CAPTURES_WAITING) return;
      const site = this.site;
      const send = async () => {
        const payload = {
          token: capture.token,
          status: answer.status,
          headers: captureHeaders(answer.headers),
          body: base64Bytes(answer.body),
          size: answer.size,
          truncated: answer.truncated,
          ttfb_ms: Math.round(answer.ttfbMs),
        };
        const response = await this.post(`/v1/gate/sites/${site.gatewayId}/captures`, payload, CAPTURE_TIMEOUT_MS);
        discard(response);
      };
      this.captureQueue.push(send);
      const drain = this.drainCaptures();
      if (waitUntil) waitUntil(drain);
    } catch (error) {
      this.report(error);
    }
  }

  private async drainCaptures(): Promise<void> {
    // A drain quiet for longer than a capture's timeout was cancelled by its runtime (Workers): start another.
    if (this.drain && this.now() - this.drain.active < CAPTURE_TIMEOUT_MS * 2) return;
    const drain = { active: this.now() };
    this.drain = drain;
    try {
      while (this.captureQueue.length && this.drain === drain) {
        const next = this.captureQueue.shift()!;
        await next().catch((error: unknown) => this.report(error)); // a failure is dropped, never retried
        drain.active = this.now();
      }
    } finally {
      if (this.drain === drain) this.drain = null;
    }
  }

  /** A signed call whose answer is read as SPEC.md section 6 says; any failure is "unusable". */
  private async ask(path: string, payload: Record<string, unknown>, budgetMs: number): Promise<Answer> {
    try {
      const response = await this.post(path, payload, budgetMs);
      if (response.status !== 200) {
        discard(response);
        return { kind: "unusable" };
      }
      return readAnswer(200, await response.json());
    } catch (error) {
      this.report(error);
      return { kind: "unusable" };
    }
  }

  private breaker(usable: boolean, settings: GateSettings): void {
    if (usable) {
      this.failures = 0;
      this.openUntil = 0;
      return;
    }
    // The count stays when it opens: after open_seconds, the next request's outcome closes or reopens it.
    this.failures += 1;
    if (this.failures >= Math.max(1, settings.breaker.failures) || this.openUntil) this.openUntil = this.now() + settings.breaker.open_seconds * 1000;
  }

  private remember(key: string, seconds: number, max: number): void {
    this.remembered.delete(key);
    while (this.remembered.size >= Math.max(1, max)) this.remembered.delete(this.remembered.keys().next().value!);
    this.remembered.set(key, this.now() + seconds * 1000);
  }

  /** The settings: fetched on the first request, then in the background every refresh_seconds (SPEC.md section 3). */
  private async loadedSettings(waitUntil?: WaitUntil): Promise<GateSettings | null> {
    // A load past its own timeout never ends (a runtime cancelled the request that started it): start another.
    const fresh = !this.settingsLoading || this.now() - this.settingsLoadingAt > SETTINGS_TIMEOUT_MS + 500;
    if (!this.settingsAt) {
      if (fresh) this.background(this.startSettings(), waitUntil);
      await withinBudget(this.settingsLoading!, SETTINGS_TIMEOUT_MS + 500);
      return this.settings;
    }
    const every = this.settings ? Math.max(this.settings.refresh_seconds * 1000, MIN_REFRESH_MS) : RETRY_WITHOUT_SETTINGS_MS;
    if (fresh && this.now() - this.settingsAt >= every) this.background(this.startSettings(), waitUntil);
    return this.settings;
  }

  private startSettings(): Promise<void> {
    this.settingsLoadingAt = this.now();
    const loading: Promise<void> = this.loadSettings(++this.settingsStarted).finally(() => {
      if (this.settingsLoading === loading) this.settingsLoading = null;
    });
    return (this.settingsLoading = loading);
  }

  /** A load's answer applies unless a newer load's already did; its failure counts only if it's the latest started. */
  private async loadSettings(generation: number): Promise<void> {
    const applies = () => generation > this.settingsApplied && ((this.settingsApplied = generation), true);
    const site = this.site!;
    const path = `/v1/gate/sites/${site.gatewayId}/settings`;
    try {
      const headers = { ...(await signedHeaders(site.secret, "GET", path, EMPTY, this.wall())), "Stilebase-Client": this.client };
      const answer = await this.fetch(this.api + path, { headers, signal: AbortSignal.timeout(SETTINGS_TIMEOUT_MS) });
      if (answer.status !== 200) discard(answer);
      if (answer.status === 404) {
        if (!applies()) return;
        this.settings = null; // the gateway was removed: everyone passes
        this.settingsAt = this.now();
        throw new Error("Stilebase has no gateway for this site key (404): every request passes");
      }
      // Any other failure keeps the last good settings (a 401 after a rotated key, a 5xx, a timeout).
      if (answer.status !== 200) throw new Error(`Stilebase answered the settings ${answer.status}`);
      const document: unknown = await answer.json();
      if (!isRecord(document)) throw new Error("Stilebase's settings aren't a JSON object: keeping the last good ones");
      const settings = parseSettings(document);
      if (applies()) {
        this.settings = settings; // settings this version can't read: everyone passes (SPEC.md section 3)
        this.settingsAt = this.now();
      }
      if (!settings) throw new Error("Stilebase sent settings this version can't read: every request passes");
    } catch (error) {
      this.report(error);
      if (generation === this.settingsStarted) this.settingsAt = this.now(); // tried: retried when due
    }
  }

  /** A signed POST to Stilebase. */
  private async post(path: string, payload: unknown, timeoutMs: number): Promise<Response> {
    const body = new TextEncoder().encode(JSON.stringify(payload));
    const headers = {
      "content-type": "application/json",
      "Stilebase-Client": this.client,
      ...(await signedHeaders(this.site!.secret, "POST", path, body, this.wall())),
    };
    return this.fetch(this.api + path, { method: "POST", body, headers, signal: AbortSignal.timeout(timeoutMs) });
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
      if (now - (this.warned.get(err.message) ?? Number.NEGATIVE_INFINITY) < 60_000) return;
      if (this.warned.size >= 100) this.warned.clear();
      this.warned.set(err.message, now);
      console.warn(`[@stilebase/sdk] ${err.message}`);
    } catch {
      // a failing error handler mustn't break the site either
    }
  }
}

/** Stilebase's respond, as SPEC.md section 10 sends it. */
function respond(status: number, headers: Record<string, string>, body: string): Respond {
  return { action: "respond", ...respondAnswer(status, headers, body) };
}

/** A payment whose outcome isn't known: never "not charged", never the page. */
export function unknownPayment(retryAfter: number): Respond {
  return { action: "respond", ...unknownPaymentAnswer(retryAfter) };
}

/** A clientIp's answer: a string, or no address (a promise's rejection is handled, never left to stop the process). */
export function addressOf(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (value && typeof (value as { then?: unknown }).then === "function") (value as PromiseLike<unknown>).then(undefined, () => undefined);
  return null;
}

/** The visitor's address from a header your proxy sets: from a list (X-Forwarded-For) its last entry, the one your
 * proxy added; the ones before it are the visitor's to write. */
export function addressFrom(value: string | null | undefined): string | null {
  return value?.split(",").pop()?.trim() || null;
}

/** Releases an answer's body the gate doesn't read (on Node an unread body holds its connection). */
function discard(answer: Response): void {
  answer.body?.cancel().catch(() => undefined);
}

function safe<T>(work: () => T, fallback: T): T {
  try {
    return work();
  } catch {
    return fallback;
  }
}

/** The promise's value, or `late` (by default an unusable answer) once `ms` have passed; the promise isn't cancelled. */
async function withinBudget<T>(promise: Promise<T>, ms: number, late: Answer = { kind: "unusable" }): Promise<T | Answer> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<Answer>((resolve) => {
    timer = setTimeout(() => resolve(late), ms);
  });
  try {
    return await Promise.race([promise, deadline]);
  } finally {
    clearTimeout(timer);
  }
}

/** STILEBASE_DISABLED: 1, true, yes or on. */
export function switchedOff(value: string | undefined): boolean {
  return ["1", "true", "yes", "on"].includes((value ?? "").trim().toLowerCase());
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

export { isRecord };
