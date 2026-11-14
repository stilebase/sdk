// Who is asking: an AI agent, a script, a browser that doesn't act like one. The checks in Stilebase's Cloudflare
// Worker, driven by the settings Stilebase sends (GateConfig) instead of lists baked in. conformance/vectors.json holds
// the cases every implementation must agree on.

/** The settings a gate runs on: GET /v1/x402/sites/{id}/config (SPEC.md, "The settings"). */
export interface GateConfig {
  config_version: number;
  refresh_seconds: number;
  gateway: { id: string; domain: string; live: boolean; reason: string | null };
  verification: { path: string; value: string } | null;
  agents: string[];
  http_clients: string[];
  known_bots: string[];
  browser_checks: { weights: Record<string, number>; suspect_score: number; cloud_asns: number[] };
  pass: { cookie: string; max_age_seconds: number };
}

/** What a gate needs to know about a request, whatever the framework. */
export interface GateRequest {
  method: string;
  /** The URL as the visitor asked for it: its scheme says whether they came over HTTPS. */
  url: string;
  headers: { get(name: string): string | null | undefined };
  /** The visitor's address, as far as it can be trusted (see the ipHeader option). */
  ip?: string | null;
  /** What the edge knows (Cloudflare's request.cf): "HTTP/1.1", "HTTP/2"…, and the network's AS number. */
  httpProtocol?: string | null;
  asn?: number | null;
}

export const CONFIG_VERSION = 1; // the settings this version reads; another means they changed meaning

/** A settings document from Stilebase, checked enough to run on; null when it isn't one. */
export function parseConfig(value: unknown): GateConfig | null {
  if (typeof value !== "object" || value === null) return null;
  const c = value as Partial<GateConfig>;
  if (c.config_version !== CONFIG_VERSION) return null;
  if (!c.gateway || typeof c.gateway.domain !== "string" || typeof c.gateway.live !== "boolean") return null;
  if ([c.agents, c.http_clients, c.known_bots].some((list) => !Array.isArray(list) || list.some((t) => typeof t !== "string"))) return null;
  const checks = c.browser_checks;
  if (!checks || typeof checks.suspect_score !== "number" || typeof checks.weights !== "object" || checks.weights === null) return null;
  if (!c.pass || typeof c.pass.cookie !== "string" || typeof c.pass.max_age_seconds !== "number") return null;
  return {
    ...(c as GateConfig),
    refresh_seconds: typeof c.refresh_seconds === "number" && c.refresh_seconds > 0 ? c.refresh_seconds : 300,
    browser_checks: { ...checks, cloud_asns: Array.isArray(checks.cloud_asns) ? checks.cloud_asns : [] },
  };
}

/** Whether `userAgent` names any of `tokens`, case-insensitively, anywhere in it. */
function names(userAgent: string, tokens: readonly string[]): boolean {
  const lowered = userAgent.toLowerCase();
  return tokens.some((token) => token && lowered.includes(token.toLowerCase()));
}

/** An AI agent by its user agent. */
export function isAiAgent(userAgent: string, config: GateConfig): boolean {
  return names(userAgent, config.agents);
}

/** A script by its default user agent, or none at all: no person browses with one. */
export function isScript(userAgent: string, config: GateConfig): boolean {
  return !userAgent.trim() || names(userAgent, config.http_clients);
}

/** The header checks a browser-looking GET fails, when they add up to a suspect; [] otherwise. */
export function suspectSignals(request: GateRequest, config: GateConfig): string[] {
  const headers = request.headers;
  const userAgent = headers.get("user-agent") ?? "";
  if (!["GET", "HEAD"].includes(request.method.toUpperCase()) || !userAgent.startsWith("Mozilla/")) return [];
  if (names(userAgent, config.known_bots)) return [];
  // Browsers send Sec-Fetch-* and client hints only over HTTPS: over plain HTTP every real Chrome would fail the checks.
  if (!request.url.toLowerCase().startsWith("https:")) return [];
  const fired: string[] = [];
  if (!headers.get("accept-language")) fired.push("no_accept_language");
  if (!["sec-fetch-mode", "sec-fetch-site", "sec-fetch-dest"].some((h) => headers.get(h))) fired.push("no_sec_fetch");
  if (/Chrome\/\d/.test(userAgent) && !headers.get("sec-ch-ua")) fired.push("chrome_without_client_hints");
  const accept = (headers.get("accept") ?? "").trim();
  if (!accept || accept === "*/*") fired.push("accept_any");
  if (/^HTTP\/1/i.test(request.httpProtocol ?? "")) fired.push("http1");
  if (request.asn != null && config.browser_checks.cloud_asns.includes(request.asn)) fired.push("cloud_asn");
  const weights = config.browser_checks.weights;
  const score = fired.reduce((sum, signal) => sum + (weights[signal] ?? 0), 0);
  return score >= config.browser_checks.suspect_score ? fired : [];
}

export interface Network {
  version: 4 | 6;
  shift: bigint;
  base: bigint;
}

/** An IPv4 or IPv6 address as a number, or null. */
export function parseIp(text: string): { version: 4 | 6; value: bigint } | null {
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(text)) {
    const parts = text.split(".").map(Number);
    if (parts.some((p) => p > 255)) return null;
    return { version: 4, value: parts.reduce((n, p) => (n << 8n) | BigInt(p), 0n) };
  }
  if (!/^[0-9a-f:]+$/i.test(text) || !text.includes(":")) return null;
  const [head, tail, extra] = text.split("::");
  if (extra !== undefined) return null;
  const left = head ? head.split(":") : [];
  const right = tail ? tail.split(":") : [];
  const missing = 8 - left.length - right.length;
  if (missing < 0 || (tail === undefined && missing !== 0)) return null;
  const groups = [...left, ...Array<string>(tail === undefined ? 0 : missing).fill("0"), ...right];
  if (groups.length !== 8 || groups.some((g) => !/^[0-9a-f]{1,4}$/i.test(g))) return null;
  return { version: 6, value: groups.reduce((n, g) => (n << 16n) | BigInt(parseInt(g, 16)), 0n) };
}

/** A CIDR prefix ("20.15.240.64/28") as a network, or null. */
export function parseNetwork(prefix: string): Network | null {
  const [address, length] = prefix.split("/");
  const ip = parseIp(address ?? "");
  if (!ip) return null;
  const bits = ip.version === 4 ? 32 : 128;
  const size = length === undefined ? bits : /^\d{1,3}$/.test(length) ? Number(length) : NaN;
  if (!Number.isInteger(size) || size < 0 || size > bits) return null;
  const shift = BigInt(bits - size);
  return { version: ip.version, shift, base: ip.value >> shift };
}

export function inNetworks(ip: string | null | undefined, networks: readonly Network[]): boolean {
  const address = parseIp(ip ?? "");
  return !!address && networks.some((n) => n.version === address.version && address.value >> n.shift === n.base);
}

/** The visitor's address as Stilebase and the address lists expect it: IPv4-mapped IPv6 ("::ffff:8.8.8.8", how
 * dual-stack servers report IPv4 visitors) as plain IPv4, trimmed; null when there is none. */
export function normalizeIp(ip: string | null | undefined): string | null {
  const trimmed = (ip ?? "").trim();
  return /^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/i.exec(trimmed)?.[1] ?? (trimmed || null);
}

/** The request's path and query ("/listings?city=Haifa"), "/" when the URL can't be read. */
export function pathOf(url: string): string {
  try {
    const parsed = new URL(url, "http://localhost");
    return parsed.pathname + parsed.search;
  } catch {
    return "/";
  }
}

/** `value` cut so its form encoding (as URLSearchParams writes it) is at most `maxEncoded` long: the price lookup goes
 * in a URL, and proxies refuse request lines over 8 KB (a refused lookup would serve the page). */
export function fitEncoded(value: string, maxEncoded: number): string {
  const encodedLength = (text: string) => new URLSearchParams({ v: text }).toString().length - 2;
  if (encodedLength(value) <= maxEncoded) return value;
  let low = 0;
  let high = value.length;
  while (low < high) {
    const middle = (low + high + 1) >> 1;
    if (encodedLength(value.slice(0, middle)) <= maxEncoded) low = middle;
    else high = middle - 1;
  }
  return value.slice(0, low);
}
