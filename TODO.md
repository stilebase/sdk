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
- **A refused price lookup reuses the last price**, which can be another agent's per-agent price; that payment then
  fails settlement and gets the 402 again. *Waits:* it only happens when a proxy refuses a lookup, which cutting the
  lookup to Stilebase's limits now avoids.
- **The price lookup cuts the user agent by encoded length, settle by characters.** A user agent full of non-ASCII
  text before the agent's name can make the two see different agents, so different prices. Cut both the same way.
  *Waits:* it needs a contrived user agent, and the agent only gets the 402 again.
- **Cutting a user agent to 512 characters could split an emoji** where a runtime decodes headers as UTF-8. *Waits:*
  the Fetch API, Node and Python decode headers as Latin-1, so it can't happen there. Cut by code points anyway.
- **The JS and Python SDKs name a `/a/../b` page differently** (JS resolves dot segments through `new URL`). Add a
  conformance case. *Waits:* only the page's name in statistics differs, not whether it's gated.
- **Paths that resolve to the root become `//` for `paths` matching** (`/a/..`). A `paths` function checking
  `p == "/"` would miss them. *Waits:* prefixes, the documented use, are unaffected.
- **Test mode is invisible on the gateway page.** A gate in `mode: "test"` passes Stilebase's **Test now**, and the
  page then says agents get a 402 while real ones read free. Report the mode in `Stilebase-Client`. *Waits:* it goes
  with the first release, together with Stilebase showing it.

## @stilebase/sdk

- **Next.js middleware on Vercel's Edge runtime must answer within 25 seconds**, and a settlement can take longer.
  The README should recommend the Node.js runtime for middleware (Next 15.5+), or the gate in a route handler.
  *Waits:* the README change goes with the first release.
- **An end-to-end test against a running Stilebase API.** *Waits:* the Python SDK has one in Stilebase's server
  suite, and both SDKs pass the same conformance cases.
