# @stilebase/sdk

Stilebase for JavaScript and TypeScript. Its first module is **the gate** (`@stilebase/sdk/gate`): AI agents pay per
request to read your site, and people pass straight through. Each module has its own entry; `import { gate } from
"@stilebase/sdk"` gathers them.

The gate answers AI agents (GPTBot, ClaudeBot, PerplexityBot…) with `402 Payment Required` in the open
[x402](https://x402.org) format. An agent that pays (USDC on Base) gets the page. [Stilebase](https://stilebase.com)
supplies the price, checks the payment and settles it, and you see every payment and agent on your dashboard.

People, search engines and link previews see no change. If Stilebase can't be reached, everyone gets your site as
usual: the gate never breaks it.

## Install

```bash
npm i @stilebase/sdk
```

Set one environment variable: the **site key** from your gateway page in Stilebase (**Reveal secret**). Keep it on
your server only.

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
// app.set("trust proxy", "loopback")  (see Express's "behind proxies"). Never trust proxies you don't run.
app.use(gate()); // before your routes
```

### Next.js

Next.js 13.5.9, 14.2.25, 15.2.3 or later: earlier versions let a request skip middleware with an
`x-middleware-subrequest` header (CVE-2025-29927), which would skip the gate.

```ts
// middleware.ts (proxy.ts from Next.js 16)
import { gate } from "@stilebase/sdk/gate/next";

export default gate();
export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };
```

### Cloudflare Workers, Hono, Bun, Deno

```ts
import { withGate } from "@stilebase/sdk/gate/fetch";

export default { fetch: withGate(handler) }; // Workers: `npx wrangler secret put STILEBASE_KEY`
// Hono:  export default { fetch: withGate(app.fetch) }
// Deno:  Deno.serve(withGate(handler))
```

### Anything else

```ts
import { createGate } from "@stilebase/sdk/gate";

const gate = createGate();
// url: the full request URL; headers: a Fetch API Headers (or anything with get(name)). From Node's req.headers (a
// plain object, not req.rawHeaders), build one: new Headers(Object.entries(req.headers).flatMap(([k, v]) =>
// v === undefined ? [] : [[k, Array.isArray(v) ? v.join(", ") : v]])).
// ip: the visitor's address (see "The visitor's address" below), or null.
const decision = await gate.check({ method, url, headers, ip });
// decision.action === "respond": send decision.status, decision.headers, decision.body instead.
// decision.action === "pass": serve the request; add PAYMENT-RESPONSE: decision.paymentResponse when it's set.
```

## Check it

On your gateway page, press **Test now**. Stilebase visits your site as its test agent and checks that it gets your
402 with the right price, and that a forged payment is refused. By hand:

```bash
curl -i -A GPTBot https://your-site.example/        # 402 with a PAYMENT-REQUIRED header
curl -i -A "Mozilla/5.0" https://your-site.example/  # your page, as before
```

When the gate first runs it fetches its settings from Stilebase, and the gateway page then shows it as **Connected**.

## What it does

- **AI agents** named by their user agent get the 402. Agents you gave a free pass on the gateway page read for free,
  but only from their operator's official addresses.
- **Payments.** A request with a payment (`PAYMENT-SIGNATURE`, or x402 v1's `X-PAYMENT`) is settled through Stilebase.
  Once settled, your page is served with `PAYMENT-RESPONSE`. A refused payment gets the 402 again, with the reason.
  If Stilebase doesn't confirm the settlement (it can't be reached, or its answer is lost), the agent gets `503` and
  the page isn't served. The payment may have settled anyway, so the gate never says it wasn't charged; sending the
  same payment again can't charge twice.
- **Agents hiding behind a browser's user agent** are asked too: requests from the addresses AI operators publish for
  their agents, or signed with Web Bot Auth's `Signature-Agent`.
- **Scripts** with their default user agent (curl, python-requests, Go-http-client…) are asked too, since no person
  browses with one.
- **Browser-looking requests that don't act like a browser** are counted on your Statistics page. With **Strict
  detection** on, they get the 402 as a page that lets real browsers straight through.
- **Domain verification.** Until you prove your domain, the gate serves `/.well-known/stilebase-verify.txt`, so you
  can verify without touching DNS.
- **Your own health checks** are scripts too (curl, wget, urllib): exempt them with `paths` (gate only your content), a
  user agent of your own on the check, or `catchHttpClients: false`.
- **`paths`** match ignoring letter case and percent-encoding, as routers do. A prefix ending in `/` also covers the
  same path without it: `"/listings/"` gates `/listings` too.

The agent lists and checks come from Stilebase. The gate refreshes them every five minutes, so new agents are caught
without a redeploy. Price changes and free passes on the gateway page apply within five minutes too.

## Options

```ts
gate({
  key: "sbk_…", // default: STILEBASE_KEY
  mode: "test", // only Stilebase's test agent is asked to pay: try it with nobody affected
  paths: ["/listings/"], // gate only these path prefixes (or a function of the path); both see it as routers do:
  //                        percent-decoded, lowercased, dot segments resolved
  ipHeader: "cf-connecting-ip", // the visitor's address from a header your proxy sets
  catchHttpClients: false, // let curl & co. through (e.g. your own monitoring)
  catchHiddenAgents: false, // only ask agents that name themselves
  checkBrowsers: false, // no browser checks
  assumeHttps: false, // the site is really served over plain http (default: visitors come over HTTPS, even when a
  //                     proxy in front ends TLS and the app sees http)
  onError: (error) => log.warn(error), // default: a console warning, once in a while
});
```

**The visitor's address** is used to recognise agents and to verify free passes:

- Express: `req.ip`, which follows Express's `trust proxy` setting. Set it to your proxies (a hop count such as `1`,
  or their addresses), not `true`: with `true`, `req.ip` is the first `X-Forwarded-For` entry, which visitors write,
  so the gate then takes the last entry instead and warns.
- Cloudflare: `CF-Connecting-IP`.
- Vercel: `x-real-ip`.

Elsewhere, name the header your proxy sets with `ipHeader`. For a list such as `X-Forwarded-For` the gate takes the
last entry, the one your proxy added. Never use a header the visitor can set themselves.

Serving directly with no proxy in front (Bun, Deno), give the gate the connection's address with `clientIp`:

```ts
// Bun
Bun.serve({ fetch: withGate(handler, { clientIp: (request, server) => server.requestIP(request)?.address ?? null }) });
// Deno
Deno.serve(withGate(handler, { clientIp: (_request, info) => info.remoteAddr.hostname }));
```

`clientIp` must return a string (or `null`), not a promise. Without an address, free passes can't verify, per-agent
prices don't apply, and agents hiding behind a browser's user agent aren't caught by their address.

## Undo

Remove the middleware. Your site is exactly as before. Or press **Pause** on the gateway page: the gate then lets
everyone through.

## Licence

MIT
