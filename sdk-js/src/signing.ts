// How a gate proves its calls come from the site (SPEC.md section 2). HMAC-SHA256 with the gateway's secret; WebCrypto
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

async function hmacHex(secret: string, message: string): Promise<string> {
  return hex(await crypto.subtle.sign("HMAC", await hmacKey(secret), encoder.encode(message)));
}

/** The headers that prove a call to Stilebase comes from your site. */
export async function signedHeaders(secret: string, method: string, path: string, body: Uint8Array, nowMs: number): Promise<Record<string, string>> {
  const ts = String(Math.floor(nowMs / 1000));
  const canonical = ["v2", ts, method.toUpperCase(), path, hex(await crypto.subtle.digest("SHA-256", body))].join("\n");
  return { "Stilebase-Timestamp": ts, "Stilebase-Signature": `v2=${await hmacHex(secret, canonical)}` };
}

/** Lowercase hex SHA-256 of bytes or a string's UTF-8 (remember keys, a held answer's receipt). */
export async function sha256Hex(data: Uint8Array | string): Promise<string> {
  const bytes = typeof data === "string" ? encoder.encode(data) : data;
  return hex(await crypto.subtle.digest("SHA-256", bytes));
}

/** Base64 of bytes (a captured body). */
export function base64Bytes(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

function hex(buffer: ArrayBuffer): string {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
