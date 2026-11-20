# Changelog

## 0.2.1

A request carrying `Stilebase-Collect` (AgentView's collector) never shares an in-flight decision, either way:
behind a gate that can't see visitors' addresses, an agent asking for the same page at that moment would otherwise
get the collector's pass. Stilebase's Collect now needs 0.2.1 or later. (0.2.0 was never released.)

## 0.2.0

The thin gate (SPEC.md): Stilebase decides every request. The gate forwards the request line and headers to
`/v1/gate/sites/{id}/decide`, does what the answer says and fails open; agent lists, browser checks and prices are no
longer in the package. A payment is held and settled after your page answered (`/settle`), so an agent is charged only
for a page delivered. New: remembered answers, static files skipped, a circuit breaker, `capture` for AgentView,
`disabled` / `STILEBASE_DISABLED`, `assumeHttps`. Removed: `catchHttpClients`, `catchHiddenAgents`, `checkBrowsers`,
`passToken`. `createGate().check` takes `{ method, path, scheme, headers: [name, value][], ip }` and may answer `hold`. A decision
past its budget (1 second by default) isn't aborted: it finishes in the background, so its connection is kept for the
next request.

## 0.1.0

First release: the gate for Express and Fetch-API runtimes (Cloudflare Workers, Hono, Bun, Deno). It does what
Stilebase's Cloudflare Worker does, run from one site key, with its settings fetched from Stilebase.
