# @stilebase/sdk

Connect your site to [Stilebase](https://stilebase.com) from JavaScript or TypeScript. **The gate**
(`@stilebase/sdk/gate`) asks Stilebase about each request and does what it says: AI agents (GPTBot, ClaudeBot,
PerplexityBot…) get `402 Payment Required` in the open [x402](https://x402.org) format, an agent that pays (USDC on
Base) gets the page, and Stilebase decides who's an agent, supplies the price and settles the payment. People, search
engines and link previews see no change. If Stilebase can't be reached, everyone gets your site as usual.

## Install

```bash
npm i @stilebase/sdk
```

Set the **site key** from your gateway page in Stilebase (**Reveal secret**). Keep it on your server only.

```bash
STILEBASE_KEY=sbk_…
```

Node 20 or later, or any runtime with the Fetch API. The package has no dependencies.

### Express

```js
import express from "express";
import { gate } from "@stilebase/sdk/gate/express";

const app = express();
// Behind a proxy or load balancer, tell Express which one, so req.ip is the visitor's:
// app.set("trust proxy", 1)  (see Express's "behind proxies"). Never trust proxies you don't run.
app.use(gate()); // before your routes
```

### Cloudflare Workers, Hono, Bun, Deno

```ts
import { withGate } from "@stilebase/sdk/gate/fetch";

export default { fetch: withGate(handler) }; // Workers: `npx wrangler secret put STILEBASE_KEY`
// Hono:  export default { fetch: withGate(app.fetch) }
// Deno:  Deno.serve(withGate(handler))
```

The gate sees only requests that reach the handler it wraps: on Workers with static assets, set
`assets.run_worker_first = true`; on Bun, serve through `fetch`, not `routes`.

### Anything else

```ts
import { createGate } from "@stilebase/sdk/gate";

const gate = createGate();
const decision = await gate.check({ method, path, scheme, headers, ip }); // path with the query; headers: [name, value][]
// "respond": send decision.status, decision.headers, decision.body instead of the page.
// "pass":    serve the page; add decision.responseHeaders; with decision.capture, send gate.capture(...) a copy.
// "hold":    a payment. Hold your answer (at most decision.maxBytes), then
//            gate.settle(decision.ticket, { status, body }) says "send" (with responseHeaders) or "respond".
```

## Check it

On your gateway page, press **Test now**. By hand:

```bash
curl -i -A GPTBot https://your-site.example/        # 402 with a PAYMENT-REQUIRED header
curl -i -A "Mozilla/5.0" https://your-site.example/  # your page, as before
```

## What it does

The gate has no logic of its own (the spec: SPEC.md in this repository). For each request it asks Stilebase what to do,
sending the request line and headers (never cookies other than Stilebase's own, never `Authorization`, never the
body), and does what Stilebase says:

- People pass straight through; Stilebase says to remember them for a while, so their next requests aren't asked.
- AI agents, scripts and agents hiding behind a browser's user agent get the 402 with Stilebase's price, unless you
  gave them a free pass or the gateway is in watch mode. Who counts as an agent is Stilebase's to decide and update.
- A payment is verified, then **held**: your page is produced but not sent until Stilebase has settled the payment.
  The agent is charged only if your page answered 2xx; it then gets the page with `PAYMENT-RESPONSE`. If the
  settlement fails it gets the 402 again; if the outcome can't be told, `503`, never the page.
- Static files (CSS, JavaScript, images, fonts) pass without asking: agents read them without paying. Remove an
  extension on the gateway page if a route of yours answers it with content.
- With AgentView, Stilebase may ask for a copy of some answers (to people who aren't signed in, and to agents); the
  copy is sent in the background and never delays or changes the page.
- If Stilebase can't be reached or answers late (1 second), the request passes. After several failures in a row the
  gate stops asking for 30 seconds.

## Options

```ts
gate({
  key: "sbk_…", // default: STILEBASE_KEY
  ipHeader: "cf-connecting-ip", // the visitor's address from a header your proxy or CDN sets
  assumeHttps: false, // send the scheme your app sees (default: https, as behind a TLS-terminating proxy)
  disabled: true, // pass everyone, ask nothing (default: STILEBASE_DISABLED=1)
  onError: (error) => log.warn(error), // default: a console warning, at most once a minute
});
```

**The visitor's address:** Express's `req.ip`; `CF-Connecting-IP` on Cloudflare; elsewhere the header your proxy sets
(`ipHeader`; from a list such as `X-Forwarded-For`, its last entry), or `clientIp` (Bun:
`(request, server) => server.requestIP(request)?.address`; Deno: `(request, info) => info.remoteAddr.hostname`). Never a
header the visitor can set.

## Undo

Remove the middleware, or press **Pause** on the gateway page.

## Licence

MIT
