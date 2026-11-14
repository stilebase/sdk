// How a gate proves its calls come from the site (SPEC.md, "Signing calls to Stilebase"):
// HMAC-SHA256 with the gateway's secret, over "v2\n<timestamp>\n<METHOD>\n<path>\n<hex SHA-256 of the body>". WebCrypto
// only, so it runs on Node 20+, Workers, Deno and Bun alike.

const encoder = new TextEncoder();
const keys = new Map<string, Promise<CryptoKey>>();

/** The gateway's id and signing secret in a site key (`sbk_<32 hex>_<64 hex>`), or null when it isn't one. */
export function parseSiteKey(key: string): { gatewayId: string; secret: string } | null {
  const match = /^sbk_([0-9a-f]{32})_([0-9a-f]{64})$/.exec(key.trim());
  if (!match) return null;
  const hex = match[1]!;
  const gatewayId = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  return { gatewayId, secret: `sbsec_${match[2]!}` };
}

function hmacKey(secret: string): Promise<CryptoKey> {
  let key = keys.get(secret);
  if (!key) {
    key = crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    keys.set(secret, key);
  }
  return key;
}

export async function hmacHex(secret: string, message: string): Promise<string> {
  return hex(await crypto.subtle.sign("HMAC", await hmacKey(secret), encoder.encode(message)));
}

export async function sha256Hex(body: Uint8Array): Promise<string> {
  return hex(await crypto.subtle.digest("SHA-256", body));
}

/** The headers that prove a call to Stilebase's x402 API comes from your site. */
export async function signedHeaders(
  secret: string,
  method: string,
  path: string,
  body: Uint8Array,
  nowMs: number,
): Promise<Record<string, string>> {
  const ts = String(Math.floor(nowMs / 1000));
  const canonical = ["v2", ts, method.toUpperCase(), path, await sha256Hex(body)].join("\n");
  return { "Stilebase-Timestamp": ts, "Stilebase-Signature": `v2=${await hmacHex(secret, canonical)}` };
}

/** The browser check's proof: when it was issued, and an HMAC of that time and the user agent with your secret. */
export async function passToken(secret: string, userAgent: string, timestamp: number): Promise<string> {
  return `${timestamp}.${(await hmacHex(secret, `stilebase-pass\n${timestamp}\n${userAgent}`)).slice(0, 32)}`;
}

/** How many pass cookies of one request are checked at most (SPEC.md, section 5, step 7). */
export const MAX_PASS_CANDIDATES = 4;

/** A browser that passed the check within `maxAgeSeconds` carries the cookie it earned (the time no more than a minute ahead). */
export async function hasPass(
  cookieHeader: string,
  cookieName: string,
  secret: string,
  userAgent: string,
  nowMs: number,
  maxAgeSeconds: number,
): Promise<boolean> {
  // Split at `,` too: a runtime may join several Cookie fields (HTTP/2 sends each cookie as its own) with ", ". Every
  // piece with the cookie's name is a candidate: one planted before the real one (another cookie's value holding a
  // comma, a sibling subdomain's cookie) mustn't hide it. Only the first few distinct, well-formed, in-time ones get an
  // HMAC, so a Cookie header stuffed with them can't make one request cost hundreds.
  const now = Math.floor(nowMs / 1000);
  const candidates = new Set<string>();
  for (const piece of cookieHeader.split(/[;,]/)) {
    const c = piece.trim();
    if (!c.startsWith(`${cookieName}=`)) continue;
    const token = c.slice(cookieName.length + 1);
    if (!/^\d{1,12}\.[0-9a-f]{32}$/.test(token)) continue;
    const age = now - Number(token.split(".")[0]);
    if (age < -60 || age > maxAgeSeconds) continue;
    candidates.add(token);
    if (candidates.size === MAX_PASS_CANDIDATES) break;
  }
  for (const token of candidates) {
    if (sameText(token, await passToken(secret, userAgent, Number(token.split(".")[0])))) return true;
  }
  return false;
}

/** Equal strings, compared in time that doesn't depend on where they differ (no guessing a token one character at a time). */
function sameText(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let difference = 0;
  for (let i = 0; i < a.length; i += 1) difference |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return difference === 0;
}

function hex(buffer: ArrayBuffer): string {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Base64 of a string's UTF-8 bytes (the PAYMENT-REQUIRED header). */
export function base64(text: string): string {
  const bytes = encoder.encode(text);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}
