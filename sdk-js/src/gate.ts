// @stilebase/sdk/gate: Stilebase decides each request your site gets (AI agents pay per request with x402, USDC on
// Base; people pass); the gate forwards, obeys and never breaks the site (SPEC.md). Adapters:
// "@stilebase/sdk/gate/express", "@stilebase/sdk/gate/fetch". This entry is the framework-neutral gate.

export { DEFAULT_API_URL, createGate } from "./core.js";
export type { CapturedAnswer, Gate, GateDecision, GateOptions, SettleDecision, Ticket, WaitUntil } from "./core.js";
export type { Capture, GateRequest, GateSettings } from "./protocol.js";
export { parseSiteKey, signedHeaders } from "./signing.js";
export { VERSION } from "./version.js";
