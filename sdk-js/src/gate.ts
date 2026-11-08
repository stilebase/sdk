// @stilebase/sdk/gate: AI agents pay per request with x402 (USDC on Base), settled through Stilebase; people pass.
// Adapters: "@stilebase/sdk/gate/express", "@stilebase/sdk/gate/next", "@stilebase/sdk/gate/fetch". This entry is the
// framework-neutral gate, for anything else.

export { DEFAULT_API_URL, addressFrom, createGate } from "./core.js";
export type { Gate, GateDecision, GateOptions, WaitUntil } from "./core.js";
export type { GateConfig, GateRequest } from "./detect.js";
export { NetworkIndex, inNetworks, isAiAgent, isScript, normalizeIp, parseIp, parseNetwork, requestUrl, suspectSignals } from "./detect.js";
export { parseSiteKey, passToken, signedHeaders } from "./signing.js";
export { VERSION } from "./version.js";
