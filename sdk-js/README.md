# @stilebase/sdk

Connect your site to [Stilebase](https://stilebase.com) from JavaScript or TypeScript. **The gate**
(`@stilebase/sdk/gate`) does what Stilebase's Cloudflare Worker does, from inside your app: AI agents (GPTBot,
ClaudeBot, PerplexityBot…) get `402 Payment Required` in the open [x402](https://x402.org) format, an agent that pays
(USDC on Base) gets the page, and Stilebase supplies the price and settles the payment. People, search engines and
link previews see no change. If Stilebase can't be reached, everyone gets your site as usual.

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
const decision = await gate.check({ method, url, headers, ip }); // headers: anything with get(name)
// decision.action === "respond": send decision.status, decision.headers, decision.body instead.
// decision.action === "pass": serve the request; add PAYMENT-RESPONSE: decision.paymentResponse when it's set.
```

## Check it

On your gateway page, press **Test now**. By hand:

```bash
curl -i -A GPTBot https://your-site.example/        # 402 with a PAYMENT-REQUIRED header
curl -i -A "Mozilla/5.0" https://your-site.example/  # your page, as before
```

## What it does

Exactly what the Worker does, with its settings (agent lists, checks, price) fetched from Stilebase and refreshed
every five minutes:

- AI agents named by their user agent, scripts with their default user agent (curl, python-requests…), and agents
  behind a browser's user agent (the operators' published addresses, Web Bot Auth) get the 402. Agents you gave a
  free pass on the gateway page read for free.
- A payment (`PAYMENT-SIGNATURE`, or x402 v1's `X-PAYMENT`) is settled through Stilebase; the page is then served with
  `PAYMENT-RESPONSE`. If Stilebase doesn't confirm the settlement, the agent gets `503` and the page isn't served.
- Browser-looking requests that don't act like a browser are counted; with **Strict detection** on, they get a page
  that lets real browsers straight through.
- Until your domain is verified, the gate serves `/.well-known/stilebase-verify.txt`.

Your own health checks and webhooks are scripts too: give them a user agent of your own, register those routes before
the gate, or set `catchHttpClients: false`.

## Options

```ts
gate({
  key: "sbk_…", // default: STILEBASE_KEY
  ipHeader: "cf-connecting-ip", // the visitor's address from a header your proxy or CDN sets
  catchHttpClients: false, // let curl & co. through
  catchHiddenAgents: false, // only ask agents that name themselves
  checkBrowsers: false, // no browser checks
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
