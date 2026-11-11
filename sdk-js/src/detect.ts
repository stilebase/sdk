// Who is asking: an AI agent, a script, a browser that doesn't act like one. A port of the checks in Stilebase's
// Cloudflare Worker, driven by the settings Stilebase sends (GateConfig) instead of copied lists.
// conformance/vectors.json holds the cases every implementation must agree on.

/** The settings a gate runs on: GET /v1/x402/sites/{id}/config (SPEC.md, "The settings"). */
export interface GateConfig {
  config_version: number;
  refresh_seconds: number;
  gateway: { id: string; domain: string; live: boolean; reason: string | null };
  verification: { path: string; value: string } | null;
  agents: string[];
  test_mode_agents: string[];
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

/** Whether `userAgent` names any of `tokens`, case-insensitively, anywhere in it. */
export function names(userAgent: string, tokens: readonly string[]): boolean {
  const lowered = userAgent.toLowerCase();
  return tokens.some((token) => token && lowered.includes(token.toLowerCase()));
}

/** An AI agent by its user agent (in test mode, only Stilebase's test agent). */
export function isAiAgent(userAgent: string, config: GateConfig, testMode = false): boolean {
  return names(userAgent, testMode ? config.test_mode_agents : config.agents);
}

/** A script by its default user agent, or none at all: no person browses with one. */
export function isScript(userAgent: string, config: GateConfig): boolean {
  return !userAgent.trim() || names(userAgent, config.http_clients);
}

/** The header checks a browser-looking GET fails, when they add up to a suspect; [] otherwise. `assumeHttps`: the
 * visitor came over HTTPS whatever the URL says (behind a proxy that ends TLS, the app sees http). */
export function suspectSignals(request: GateRequest, config: GateConfig, assumeHttps = false): string[] {
  const headers = request.headers;
  const userAgent = headers.get("user-agent") ?? "";
  if (!["GET", "HEAD"].includes(request.method.toUpperCase()) || !userAgent.startsWith("Mozilla/")) return [];
  if (names(userAgent, config.known_bots)) return [];
  // Browsers send Sec-Fetch-* and client hints only over HTTPS: over plain HTTP every real Chrome would fail the checks.
  if (!assumeHttps && !request.url.toLowerCase().startsWith("https:")) return [];
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

export interface IpAddress {
  version: 4 | 6;
  value: bigint;
}

export interface Network {
  version: 4 | 6;
  shift: bigint;
  base: bigint;
}

/** An IPv4 or IPv6 address as a number, or null. */
export function parseIp(text: string): IpAddress | null {
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
  const size = length === undefined ? bits : /^\d{1,3}$/.test(length) ? Number(length) : NaN; // "1.2.3.4/" isn't /0
  if (!Number.isInteger(size) || size < 0 || size > bits) return null;
  const shift = BigInt(bits - size);
  return { version: ip.version, shift, base: ip.value >> shift };
}

/** The visitor's address as Stilebase and the address lists expect it: IPv4-mapped IPv6 ("::ffff:8.8.8.8", how
 * dual-stack servers report IPv4 visitors) as plain IPv4, trimmed; null when there is none. */
export function normalizeIp(ip: string | null | undefined): string | null {
  const trimmed = (ip ?? "").trim();
  return /^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/i.exec(trimmed)?.[1] ?? (trimmed || null);
}

export function inNetworks(ip: string | null | undefined, networks: readonly Network[] | NetworkIndex): boolean {
  const address = parseIp(ip ?? "");
  if (!address) return false;
  if (networks instanceof NetworkIndex) return networks.has(address);
  return networks.some((n) => n.version === address.version && address.value >> n.shift === n.base);
}

/** Networks grouped by prefix length, so a lookup costs one set probe per distinct length (a few dozen), not one
 * comparison per network (thousands): it runs for every request from a person. */
export class NetworkIndex {
  private readonly groups: { version: 4 | 6; shift: bigint; bases: Set<bigint> }[] = [];

  constructor(networks: readonly Network[]) {
    const byLength = new Map<string, { version: 4 | 6; shift: bigint; bases: Set<bigint> }>();
    for (const n of networks) {
      const key = `${n.version}/${n.shift}`;
      let group = byLength.get(key);
      if (!group) byLength.set(key, (group = { version: n.version, shift: n.shift, bases: new Set() }));
      group.bases.add(n.base);
    }
    this.groups = [...byLength.values()];
  }

  has(address: IpAddress): boolean {
    return this.groups.some((g) => g.version === address.version && g.bases.has(address.value >> g.shift));
  }
}

// A host name (underscores too: Docker Compose service names), or a bracketed IPv6 literal, and an optional port.
const HOST = /^(?:[a-z0-9_-]+(?:\.[a-z0-9_-]+)*|\[[0-9a-f:.]+\])(?::\d{1,5})?$/i;
const SCHEME_AND_AUTHORITY = /^([a-z][a-z0-9+.-]*):\/\/([^/?#]*)(.*)$/is;

/** The URL a visitor asked for, from parts a framework gives: the Host header is the visitor's to write, so anything
 * that isn't a host name (a "?" or "#" would change the path the gate reads) becomes "localhost", and an absolute-form
 * request target keeps only its path and query, as written (cut as text, never resolved by a URL parser). */
export function requestUrl(scheme: string, host: string | null | undefined, target: string): string {
  const safeHost = host && HOST.test(host) ? host : "localhost";
  let path = target || "/";
  if (!path.startsWith("/")) {
    // An absolute-form target keeps only its path, but only when its host part is a plain host name: Node's url.parse
    // (Express) ends a host part at `%`, `;` or `'`, so `http://x%2Fpremium/...` is a different path there. Anything
    // else stays whole, as a path no free path can match.
    const parts = SCHEME_AND_AUTHORITY.exec(path);
    path = parts && HOST.test(parts[2]!) ? parts[3] || "/" : `/${encodeURIComponent(path)}`;
    if (!path.startsWith("/")) path = `/${path}`;
  }
  return `${scheme === "https" ? "https" : "http"}://${safeHost}${path.replaceAll("#", "%23")}`;
}

export const CONFIG_VERSION = 1; // the settings this version reads; another means they changed meaning

/** A settings document from Stilebase, checked enough to run on; null when it isn't one. */
export function parseConfig(value: unknown): GateConfig | null {
  if (typeof value !== "object" || value === null) return null;
  const c = value as Partial<GateConfig>;
  if (c.config_version !== CONFIG_VERSION) return null;
  const lists = [c.agents, c.test_mode_agents, c.http_clients, c.known_bots];
  if (!c.gateway || typeof c.gateway.domain !== "string" || typeof c.gateway.live !== "boolean") return null;
  if (lists.some((list) => !Array.isArray(list) || list.some((t) => typeof t !== "string"))) return null;
  const checks = c.browser_checks;
  if (!checks || !isNumber(checks.suspect_score) || typeof checks.weights !== "object" || checks.weights === null) return null;
  if (!c.pass || typeof c.pass.cookie !== "string" || !isNumber(c.pass.max_age_seconds)) return null;
  return {
    ...(c as GateConfig),
    refresh_seconds: isNumber(c.refresh_seconds) && c.refresh_seconds > 0 ? c.refresh_seconds : 300,
    browser_checks: { ...checks, cloud_asns: Array.isArray(checks.cloud_asns) ? checks.cloud_asns.filter(isNumber) : [] },
  };
}

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** A path every parser reads the same way: from `/`, only letters, digits and `-` `.` `_` `~` between slashes, no
 * empty, `.` or `..` segment (a trailing slash is fine). No percent-encoding, so nothing to decode or resolve. */
export function isLiteralPath(path: string): boolean {
  if (!/^\/[A-Za-z0-9._~/-]*$/.test(path)) return false;
  const segments = path.slice(1).split("/");
  if (segments.at(-1) === "") segments.pop(); // a trailing slash
  return segments.every((segment) => segment !== "" && segment !== "." && segment !== "..");
}

/** The path of a request URL when the whole URL is literal: http(s), a plain host name, then a literal path (a query
 * may follow). Null for anything else (a Host like "x?", percent-encoding, dot segments, an odd scheme): such a request
 * is never free, since some router may read it differently. */
export function literalPath(url: string): string | null {
  const parts = /^https?:\/\/([^/?#]*)(\/[^?#]*)(?:[?#].*)?$/is.exec(url);
  if (!parts || !HOST.test(parts[1]!) || !isLiteralPath(parts[2]!)) return null;
  return parts[2]!;
}

/** The page as the gate names it to Stilebase and agents: the gateway's own domain over https, with the request's path
 * and query. The Host header is the visitor's to write, and behind a proxy it's an internal name (app:3000): neither
 * belongs in a 402's resource URL or the Bazaar. Never throws, whatever the adapter was given. */
export function siteUrl(raw: string, domain: string): URL {
  let rest = /^[a-z][a-z0-9+.-]*:\/\/[^/?#]*(.*)$/is.exec(raw)?.[1] ?? raw;
  rest = rest.replace(/#.*$/s, "").replaceAll("\\", "%5C"); // no fragment; a backslash would read as a slash
  rest = `/${rest.replace(/^\/+/, "")}`; // "//evil.example/x" is a path here, never a host
  try {
    return new URL(rest, `https://${domain}`);
  } catch {
    return new URL(`https://${domain}/`);
  }
}

/** `value` cut so its percent-encoding is at most `maxEncoded` long: the price lookup goes in a URL, and proxies refuse
 * request lines over 8 KB (a refusal must never serve the page). */
export function fitEncoded(value: string, maxEncoded: number): string {
  // As URLSearchParams writes it (form encoding: "(" is 3 characters there, 1 in encodeURIComponent).
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
