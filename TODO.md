# TODO

What's known and not yet done, each with why it waits. Findings from code reviews that aren't blocking
(`.claude/rules/development-workflow.md`) go here.

## Both SDKs

- **Conformance cases for the newer rules:** the cut-to-fit of `url` and `ua` (WHATWG encoding, `~` and `*`), the
  refusals answered at the last price (429 included), `refresh_seconds` defaults, settle answers that aren't a JSON
  object or whose `success` isn't a boolean. *Waits:* each SDK has its own tests for them; shared cases come next.
- **A refused lookup for a suspect** (strict detection off) is answered at the last price, where a served suspect is
  the normal answer; and the last price lives in memory, so a fresh serverless instance has none. Put the gateway's
  base requirement in the signed settings, and serve suspects on a refusal. *Waits:* refusals no longer happen in
  normal operation (the lookup is cut to Stilebase's limits).
- **A failed first fetch of the address lists** leaves them empty for an hour. Retry every 30 seconds while they're
  empty after a failure, as with the settings. *Waits:* it needs Stilebase to fail exactly at a gate's start, and
  agents are still asked to pay by user agent.
- **Any 404 on the settings drops them**, a routing fault at Stilebase's edge included. Drop them only on the API's
  own "no such gateway" answer. *Waits:* the API answers 404 only for removed gateways.
- **The browser check's pass token is in the 402 page**, so a script that reads the page can lift it. A stronger
  challenge is a design change to strict detection. *Waits:* strict detection is off by default.

- **Warn once when the gate has no visitor address** (the fetch adapter outside Cloudflare, Next.js outside Vercel,
  without `ipHeader`). Free passes then never verify and hidden agents aren't caught by address, silently. *Waits:* the
  READMEs say to set it; agents are still asked to pay by user agent.
- **Python: the timeouts are per network step, not a total deadline.** httpx applies 3, 5 and 65 seconds to each of
  connect, write and read, so a slow or trickling answer can hold a request longer (the JS SDK's are total). Wrap each
  call in an overall deadline. *Waits:* the far end is Stilebase's own API, which answers or fails fast.

- **More frameworks:** Fastify, Koa and Hono-native adapters for `@stilebase/sdk` (Hono works today through
  `withGate(app.fetch)`); Django's async middleware for `stilebase-sdk`. *Waits:* the adapters that exist cover the
  common stacks.
- **More languages**, each on `conformance/` and [SPEC.md](SPEC.md): Go (`net/http`, Gin, Echo, Caddy and Traefik
  plugins), PHP (Laravel, Symfony), Ruby (Rack), Java/Kotlin, .NET, Rust. *Waits:* demand decides the order.
